//! Replicated cluster configuration entries.

mod support;

use rafty::cluster::{ClusterMembership, ConfigChange, ConfigurationEntry};
use rafty::node_state::{Node, State, states::Leader};
use rafty::remote_node::ClusterClient;
use rafty::transport::model::ClusterMessage;
use rafty::transport::model::membership::ConfigurationBroadcast;
use rafty::transport::model::vote::VoteRequest;
use support::*;

use std::sync::Arc;
use std::time::Duration;

fn leadered(client: &Arc<ClusterClient>) -> State<Leader> {
    follower_state(
        "node1",
        FakeLog::empty(),
        Arc::new(FakeStorage::default()),
        Arc::clone(client),
        test_registry(),
    )
    .into_candidate()
    .into_leader()
}

#[tokio::test]
async fn configuration_change_replicates_and_applies_on_a_follower() {
    let configuration = configuration(
        "demo",
        0,
        &[
            ("node1", "node1:1001", true),
            ("node2", "node2:1002", true),
            ("node3", "node3:1003", true),
        ],
    );
    let client = cluster_client_with_configuration(
        "node1",
        configuration.clone(),
        &[("node2", Arc::new(Counters::default()), Behavior::approve())],
    );
    let node = Node::Follower(follower_state(
        "node1",
        FakeLog::empty(),
        Arc::new(FakeStorage::default()),
        Arc::clone(&client),
        test_registry(),
    ));

    let change = ConfigChange::AddLearner {
        node_id: node_id("node4"),
        address: address("node4:1004"),
    };
    let next = configuration.applied(&change).unwrap();
    let message = ClusterMessage::new(
        node_id("node3"),
        4,
        ConfigurationBroadcast {
            entry: ConfigurationEntry::new(next.index, 4, change),
            configuration: next,
        },
    );

    let ack = node.apply_configuration(&message);
    assert!(
        ack.payload.accepted,
        "unexpected rejection: {:?}",
        ack.payload.reason
    );
    assert_eq!(ack.metadata.term, 4);
    assert_eq!(client.configuration().index, 1);
    assert!(client.configuration().is_learner(&node_id("node4")));

    // Retries of the same entry are accepted and do not advance the index.
    let replay = node.apply_configuration(&message);
    assert!(replay.payload.accepted);
    assert_eq!(client.configuration().index, 1);

    // Skipping an entry is rejected: membership would otherwise miss a change.
    let gap = ClusterMessage::new(
        node_id("node3"),
        4,
        ConfigurationBroadcast {
            entry: ConfigurationEntry::new(
                3,
                4,
                ConfigChange::Promote {
                    node_id: node_id("node4"),
                },
            ),
            configuration: client.configuration().as_ref().clone(),
        },
    );
    assert!(!node.apply_configuration(&gap).payload.accepted);

    // A message from an older term is rejected without touching membership.
    let stale = ClusterMessage::new(node_id("node3"), 1, gap.payload.clone());
    assert!(!node.apply_configuration(&stale).payload.accepted);
}

#[tokio::test]
async fn a_configuration_change_without_a_quorum_is_not_applied() {
    let client = cluster_client(
        "node1",
        &[
            (
                "node2",
                Arc::new(Counters::default()),
                Behavior::rejecting_configuration(),
            ),
            (
                "node3",
                Arc::new(Counters::default()),
                Behavior::rejecting_configuration(),
            ),
        ],
    );
    let leader = leadered(&client);

    let result = client
        .change_configuration(
            ConfigChange::AddLearner {
                node_id: node_id("node4"),
                address: address("node4:1004"),
            },
            leader.term(),
        )
        .await;

    assert!(
        result.is_err(),
        "the leader must not commit without a quorum"
    );
    assert_eq!(client.configuration().index, 0);
    assert!(!client.configuration().contains(&node_id("node4")));
}

#[tokio::test]
async fn a_voter_set_is_required_before_a_node_can_vote() {
    let client = Arc::new(ClusterClient::with_membership(
        node_id("node4"),
        Arc::new(ClusterMembership::new(configuration("demo", 0, &[]))),
        Arc::new(std::collections::HashMap::new()),
        Duration::from_millis(40),
    ));

    let result = client
        .ask_for_vote(ClusterMessage::new(node_id("node4"), 1, VoteRequest))
        .await;

    assert!(
        result.is_err(),
        "an empty voter set must not count as an immediate quorum"
    );
}
