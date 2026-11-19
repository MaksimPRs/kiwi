use crate::cluster::{
    ClusterConfiguration, ClusterId, ClusterMember, ClusterMembership, NodeAddress,
};
use crate::node_state::NodeId;
use crate::transport::model::command::Command;
use crate::transport::model::common::{
    CommitRequest, CommitResponse, HeartbeatRequest, HeartbeatResponse,
};
use crate::transport::model::log::{LogReplicationRequest, LogReplicationResponse};
use crate::transport::model::vote::{VoteRequest, VoteResponse};
use crate::transport::model::{ClusterMessage, NodeAware};
use anyhow::{Result, anyhow, bail};
use arc_swap::ArcSwap;
use async_trait::async_trait;
use futures::StreamExt;
use futures::stream::FuturesUnordered;
use log::warn;
use std::collections::HashMap;
use std::collections::hash_map::Keys;
use std::fmt::Debug;
use std::sync::Arc;
use std::time::Duration;
use tokio::time::timeout;

#[async_trait]
pub trait CandidateClient: Debug + Send + Sync {
    async fn ask_for_vote(&self, req: &ClusterMessage<VoteRequest>)
    -> ClusterMessage<VoteResponse>;
}

#[async_trait]
pub trait FollowerClient: Debug + Send + Sync {
    async fn request_log(
        &self,
        req: ClusterMessage<LogReplicationRequest>,
    ) -> ClusterMessage<LogReplicationResponse>;
}

#[async_trait]
pub trait LeaderClient: Debug + Send + Sync {
    async fn send_payload(&self, req: &ClusterMessage<Command>) -> ClusterMessage<()>;

    async fn commit(&self, req: &ClusterMessage<CommitRequest>) -> ClusterMessage<CommitResponse>;

    async fn heartbeat(
        &self,
        req: &ClusterMessage<HeartbeatRequest>,
    ) -> ClusterMessage<HeartbeatResponse>;
}

#[derive(Debug, Clone)]
pub struct RemoteNode {
    candidate_client: Arc<dyn CandidateClient>,
    follower_client: Arc<dyn FollowerClient>,
    leader_client: Arc<dyn LeaderClient>,
}

impl RemoteNode {
    /// Builds a remote node from its per-role transport clients.
    ///
    /// A peer may implement only a subset of the roles; in that case the remaining
    /// clients can be supplied by the same object (as an in-memory transport does).
    pub fn new(
        candidate_client: Arc<dyn CandidateClient>,
        follower_client: Arc<dyn FollowerClient>,
        leader_client: Arc<dyn LeaderClient>,
    ) -> Self {
        Self {
            candidate_client,
            follower_client,
            leader_client,
        }
    }
}

#[derive(Debug)]
pub struct ClusterClient {
    node_id: NodeId,
    membership: Arc<ClusterMembership>,
    remote_nodes: ArcSwap<HashMap<NodeId, RemoteNode>>,
    max_timeout: Duration,
}

fn accept_all<T>(_response: &T) -> bool {
    true
}

impl ClusterClient {
    fn _empty_map<K, V>() -> HashMap<K, V> {
        HashMap::<K, V>::with_capacity(0)
    }

    pub fn new(
        node_id: NodeId,
        remote_nodes: Arc<HashMap<NodeId, RemoteNode>>,
        max_timeout: Duration,
    ) -> Self {
        let members = remote_nodes
            .keys()
            .filter(|id| **id != node_id)
            .chain(std::iter::once(&node_id))
            .map(|id| ClusterMember::voter(id.clone(), NodeAddress::placeholder(id)));

        let configuration =
            ClusterConfiguration::new(ClusterId::new("anonymous").expect("static"), 0, members)
                .expect("in-memory cluster configuration is valid");
        Self::with_membership(
            node_id,
            Arc::new(ClusterMembership::new(configuration)),
            remote_nodes,
            max_timeout,
        )
    }

    pub fn with_membership(
        node_id: NodeId,
        membership: Arc<ClusterMembership>,
        remote_nodes: Arc<HashMap<NodeId, RemoteNode>>,
        max_timeout: Duration,
    ) -> Self {
        Self {
            node_id,
            membership,
            remote_nodes: ArcSwap::from(remote_nodes),
            max_timeout,
        }
    }

    pub fn configuration(&self) -> Arc<ClusterConfiguration> {
        self.membership.configuration()
    }

    pub fn register_remote(&self, node_id: NodeId, remote: RemoteNode) {
        let current = self.remote_nodes.load_full();
        let mut next = (*current).clone();
        next.insert(node_id, remote);
        self.remote_nodes.store(Arc::new(next));
    }

    pub async fn heartbeat(&self, request: ClusterMessage<HeartbeatRequest>) -> Result<()> {
        let expected = self
            .remote_nodes
            .load()
            .keys()
            .filter(|id| **id != self.node_id)
            .count();
        self.broadcast_call(
            &request,
            async |node, req| node.leader_client.heartbeat(req).await,
            accept_all,
            move |succeeded| succeeded.len() >= expected,
            false,
        )
        .await
        .map(|_| ())
    }

    pub async fn push(&self, request: ClusterMessage<Command>) -> Result<()> {
        self.broadcast_call_quorum(
            &request,
            async |node, command| node.leader_client.send_payload(command).await,
            accept_all,
        )
        .await
        .map(|_| ())
    }

    pub async fn commit(&self, request: ClusterMessage<CommitRequest>) -> Result<()> {
        self.broadcast_call_quorum(
            &request,
            async |node, req| node.leader_client.commit(req).await,
            accept_all,
        )
        .await
        .map(|_| ())
    }

    pub async fn ask_for_vote(&self, request: ClusterMessage<VoteRequest>) -> Result<()> {
        self.broadcast_call_quorum(
            &request,
            async |node, req| node.candidate_client.ask_for_vote(req).await,
            |resp| resp.voted,
        )
        .await
        .map(|_| ())
    }

    pub async fn ask_for_log(
        &self,
        leader_id: NodeId,
        request: ClusterMessage<LogReplicationRequest>,
    ) -> Result<LogReplicationResponse> {
        let remotes = self.remote_nodes.load();
        let Some(leader_node) = remotes.get(&leader_id) else {
            return Err(anyhow!(
                "leader with id [{:?}] not found",
                request.node_id()
            ));
        };
        let message = leader_node.follower_client.request_log(request).await;
        Ok(message.payload)
    }

    pub fn exists(&self, node_id: &NodeId) -> bool {
        self.membership.configuration().contains(node_id)
    }

    pub fn is_voter(&self, node_id: &NodeId) -> bool {
        self.membership.configuration().is_voter(node_id)
    }

    pub fn address_of(&self, node_id: &NodeId) -> Option<NodeAddress> {
        self.membership.configuration().address_of(node_id).cloned()
    }

    async fn broadcast_call_quorum<Req: Debug, Resp: NodeAware, RemCall, SuccessPred>(
        &self,
        request: &Req,
        remote_call_fn: RemCall,
        success_predicate: SuccessPred,
    ) -> Result<HashMap<NodeId, Resp>>
    where
        RemCall: AsyncFn(&RemoteNode, &Req) -> Resp,
        SuccessPred: Fn(&Resp) -> bool,
    {
        let configuration = self.membership.configuration();
        let Some(quorum) = configuration.quorum_size() else {
            // A node that has not joined yet has no voters. Treating that as "quorum
            // already reached" would let it win an election or commit changes alone.
            bail!("cluster has no voters; the node has not joined yet")
        };
        let needed = if configuration.is_voter(&self.node_id) {
            quorum.saturating_sub(1)
        } else {
            quorum
        };
        self.broadcast_call(
            request,
            remote_call_fn,
            success_predicate,
            move |succeeded| {
                let acknowledgements = succeeded
                    .filter(|id| configuration.is_voter(id))
                    .count();
                acknowledgements >= needed
            },
            false,
        )
        .await
    }

    // FIXME Response should be a Result<Resp, Error>, so in case http call failed, it won't be classified as succeeded
    async fn broadcast_call<Req: Debug, Resp: NodeAware, RemCall, SuccessPred, QuorumPred>(
        &self,
        request: &Req,
        remote_call_fn: RemCall,
        success_criteria: SuccessPred,
        quorum_predicate: QuorumPred,
        tolerate_errors: bool,
    ) -> Result<HashMap<NodeId, Resp>>
    where
        RemCall: AsyncFn(&RemoteNode, &Req) -> Resp,
        SuccessPred: Fn(&Resp) -> bool,
        QuorumPred: Fn(Keys<NodeId, Resp>) -> bool,
    {
        let remotes = self.remote_nodes.load_full();
        let mut remote_calls = remotes
            .iter()
            .filter(|(node_id, _)| **node_id != self.node_id)
            .map(|(_, remote_node)| timeout(self.max_timeout, remote_call_fn(remote_node, request)))
            .collect::<FuturesUnordered<_>>();

        let mut succeeded_requests = HashMap::with_capacity(remote_calls.len());
        while let Some(result) = remote_calls.next().await {
            match result {
                Ok(response) => {
                    if success_criteria(&response) {
                        succeeded_requests.insert(response.node_id().clone(), response);
                    }
                }
                Err(error) if tolerate_errors => warn!("[broadcast] remote call did not finish in time: {error}"),
                Err(error) => return Err(anyhow!("remote call failed: {error}")),
            }
            // Configuration broadcasts must reach every member, so they keep collecting
            // responses even after a quorum has been reached (`tolerate_errors` is the
            // "broadcast" mode here). Quorum calls return as soon as they can decide.
            if !tolerate_errors && quorum_predicate(succeeded_requests.keys()) {
                return Ok(succeeded_requests);
            }
        }

        if quorum_predicate(succeeded_requests.keys()) {
            Ok(succeeded_requests)
        } else {
            Err(anyhow!(
                "Batch request failed. Quorum hasn't been achieved for request: {request:?}"
            ))
        }
    }
}
