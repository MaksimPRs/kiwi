//! Cluster membership: identity, addresses and replicated configuration.
//!
//! Raft distinguishes three things that are easy to conflate:
//!
//! * **Identity** — [`NodeId`] is stable for the lifetime of a node.
//! * **Address** — [`NodeAddress`] is how other nodes dial it. In Docker this is a
//!   service name, not a container IP, because container IPs change on restart.
//! * **Membership** — which nodes are voters or learners. This is replicated as a
//!   [`ConfigurationEntry`] and applied to a [`ClusterMembership`].
//!
//! [`ClusterMembership::new`] is the state a node is born with (the static config from
//! `config.yaml`). From then on, the only ways it changes are a replicated
//! [`ConfigurationEntry`] or a join snapshot received from the leader.

use crate::node_state::NodeId;
use anyhow::{Result, anyhow, bail};
use arc_swap::ArcSwap;
use http::uri::Authority;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::fmt::{Debug, Display, Formatter};
use std::str::FromStr;
use std::sync::Arc;

/// Identifier of a Raft cluster.
///
/// A node refuses to join, or to apply a configuration from, a different cluster.
/// This is what stops a restarted node with an empty data directory from silently
/// forming a second cluster that later merges with the first one.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct ClusterId(Arc<str>);

impl ClusterId {
    pub fn new(value: impl Into<String>) -> Result<Self> {
        value.into().parse()
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Display for ClusterId {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.as_str())
    }
}

impl FromStr for ClusterId {
    type Err = anyhow::Error;

    fn from_str(value: &str) -> Result<Self> {
        let value = value.trim();
        if value.is_empty() {
            bail!("cluster id must not be empty");
        }
        Ok(Self(Arc::from(value)))
    }
}

impl TryFrom<String> for ClusterId {
    type Error = anyhow::Error;

    fn try_from(value: String) -> Result<Self> {
        value.parse()
    }
}

impl From<ClusterId> for String {
    fn from(value: ClusterId) -> Self {
        value.as_str().to_owned()
    }
}

/// A dialable `host:port` pair.
///
/// This wraps [`http::uri::Authority`] instead of hand-rolling a parser: the URI
/// authority grammar already covers DNS names, IPv4 and bracketed IPv6. It is
/// serialized as a string (`"node2:1002"`).
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct NodeAddress(Authority);

impl NodeAddress {
    pub fn new(host: impl AsRef<str>, port: u16) -> Result<Self> {
        let host = host.as_ref().trim();
        if host.is_empty() {
            bail!("node address host must not be empty");
        }
        let rendered = if host.contains(':') && !host.starts_with('[') {
            format!("[{host}]:{port}")
        } else {
            format!("{host}:{port}")
        };
        rendered.parse()
    }

    /// Address used by in-process clients that do not dial a socket.
    pub fn placeholder(id: &NodeId) -> Self {
        Self::new(id.as_str(), 0).expect("node ids are valid authority hosts")
    }

    pub fn host(&self) -> &str {
        let host = self.0.host();
        host.strip_prefix('[')
            .and_then(|host| host.strip_suffix(']'))
            .unwrap_or(host)
    }

    pub fn port(&self) -> u16 {
        self.0
            .port_u16()
            .expect("a NodeAddress always contains a port")
    }

    /// A wildcard listen address (`0.0.0.0:1001`) is not a valid advertised address.
    pub fn is_dialable(&self) -> bool {
        self.port() != 0
    }
}

impl Display for NodeAddress {
    fn fmt(&self, formatter: &mut Formatter<'_>) -> std::fmt::Result {
        Display::fmt(&self.0, formatter)
    }
}

impl FromStr for NodeAddress {
    type Err = anyhow::Error;

    fn from_str(value: &str) -> Result<Self> {
        let value = value.trim();
        let authority: Authority = value
            .parse()
            .map_err(|error| anyhow!("invalid node address [{value}]: {error}"))?;
        if authority.host().is_empty() {
            bail!("invalid node address [{value}]: host must not be empty");
        }
        if authority.port_u16().is_none() {
            bail!("invalid node address [{value}]: expected host:port");
        }
        Ok(Self(authority))
    }
}

impl TryFrom<String> for NodeAddress {
    type Error = anyhow::Error;

    fn try_from(value: String) -> Result<Self> {
        value.parse()
    }
}

impl From<NodeAddress> for String {
    fn from(value: NodeAddress) -> Self {
        value.to_string()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum MemberRole {
    Voter,
    Learner,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClusterMember {
    pub id: NodeId,
    pub address: NodeAddress,
    pub role: MemberRole,
}

impl ClusterMember {
    pub fn voter(id: NodeId, address: NodeAddress) -> Self {
        Self {
            id,
            address,
            role: MemberRole::Voter,
        }
    }

    pub fn learner(id: NodeId, address: NodeAddress) -> Self {
        Self {
            id,
            address,
            role: MemberRole::Learner,
        }
    }

    pub fn is_voter(&self) -> bool {
        self.role == MemberRole::Voter
    }

    pub fn is_learner(&self) -> bool {
        self.role == MemberRole::Learner
    }
}

/// The full membership of a cluster at a given configuration index.
///
/// The index is the Raft log index of the entry that produced this configuration.
/// Bootstrap configuration has index `0`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClusterConfiguration {
    pub cluster_id: ClusterId,
    pub index: u64,
    pub members: HashMap<NodeId, ClusterMember>,
}

impl ClusterConfiguration {
    pub fn new(
        cluster_id: ClusterId,
        index: u64,
        members: impl IntoIterator<Item = ClusterMember>,
    ) -> Result<Self> {
        let mut by_id = HashMap::new();
        for member in members {
            if by_id.insert(member.id.clone(), member).is_some() {
                bail!("duplicate node id in cluster configuration");
            }
        }
        let configuration = Self {
            cluster_id,
            index,
            members: by_id,
        };
        configuration.validate()?;
        Ok(configuration)
    }

    pub fn validate(&self) -> Result<()> {
        let mut addresses = HashSet::with_capacity(self.members.len());
        for (id, member) in &self.members {
            if id != &member.id {
                bail!("member key [{id}] does not match member id [{}]", member.id);
            }
            // Placeholder addresses (used by in-process transports) are not dialable and
            // are allowed to repeat. Real addresses must be unique.
            if member.address.is_dialable() && !addresses.insert(member.address.clone()) {
                bail!("duplicate node address [{}]", member.address);
            }
        }
        Ok(())
    }

    pub fn members(&self) -> impl Iterator<Item = &ClusterMember> {
        self.members.values()
    }

    pub fn voters(&self) -> impl Iterator<Item = &ClusterMember> {
        self.members.values().filter(|member| member.is_voter())
    }

    pub fn learners(&self) -> impl Iterator<Item = &ClusterMember> {
        self.members.values().filter(|member| member.is_learner())
    }

    pub fn voter_count(&self) -> usize {
        self.voters().count()
    }

    /// Number of nodes that form a majority, including the local node when it is a voter.
    pub fn quorum_size(&self) -> Option<usize> {
        let voters = self.voter_count();
        if voters == 0 { None } else { Some(voters / 2 + 1) }
    }

    pub fn get(&self, node_id: &NodeId) -> Option<&ClusterMember> {
        self.members.get(node_id)
    }

    pub fn contains(&self, node_id: &NodeId) -> bool {
        self.members.contains_key(node_id)
    }

    pub fn is_voter(&self, node_id: &NodeId) -> bool {
        self.get(node_id).is_some_and(ClusterMember::is_voter)
    }

    pub fn is_learner(&self, node_id: &NodeId) -> bool {
        self.get(node_id).is_some_and(ClusterMember::is_learner)
    }

    pub fn address_of(&self, node_id: &NodeId) -> Option<&NodeAddress> {
        self.get(node_id).map(|member| &member.address)
    }
}

/// Thread-safe holder for the currently applied [`ClusterConfiguration`].
#[derive(Debug)]
pub struct ClusterMembership {
    configuration: ArcSwap<ClusterConfiguration>,
}

impl ClusterMembership {
    pub fn new(configuration: ClusterConfiguration) -> Self {
        Self {
            configuration: ArcSwap::from_pointee(configuration),
        }
    }

    pub fn configuration(&self) -> Arc<ClusterConfiguration> {
        self.configuration.load_full()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn node_address_round_trips_and_supports_ipv6() {
        let address: NodeAddress = "node2:1002".parse().unwrap();

        assert_eq!(address.to_string(), "node2:1002");
        assert_eq!(address.host(), "node2");
        assert_eq!(address.port(), 1002);

        let json = serde_json::to_string(&address).unwrap();
        assert_eq!(json, "\"node2:1002\"");
        assert_eq!(serde_json::from_str::<NodeAddress>(&json).unwrap(), address);

        let ipv6: NodeAddress = "[::1]:1001".parse().unwrap();
        assert_eq!(ipv6.host(), "::1");
        assert_eq!(ipv6.port(), 1001);
        assert_eq!(ipv6.to_string(), "[::1]:1001");

        assert!(
            "node2".parse::<NodeAddress>().is_err(),
            "a port is required"
        );
    }
}
