# Workspace instructions

## Build artifacts — do not read

- `target/` (at the repo root and any nested one) holds Cargo build output. Never read, glob, grep, or open files under it.
- Scope searches to source instead of scanning the repo root: `rafty/src`, `rafty/tests`, or explicit files. Do not glob/grep the repo root; `.cargo-home/` (untracked) also contains vendored crate sources.
- Never stage or commit anything from `target/`; it is already in `.gitignore`.

## Layout

- Cargo workspace with a single member: `rafty/` (the consensus library).
- `kiwi/` is not a crate in this checkout. If the directory exists locally, it contains only empty `src/` and `tests/` directories: no `Cargo.toml`, no source, and no workspace member. Older docs that mention a `kiwi` binary or HTTP server describe something that is not here yet.
- Shared dependency versions live in the root `Cargo.toml` under `[workspace.dependencies]`; add new dependencies there rather than pinning versions per crate.
- Edition 2024. The crate uses `std::range::RangeInclusive`, which requires Rust 1.96+ (CI uses stable). Tokio is the async runtime.

## Project structure

`rafty` is a request-unaware Raft library: it models the node's consensus state and the background work that drives it, and knows nothing about HTTP, DI, or a concrete network transport. The server layer is expected to own a shared `Arc<ArcSwap<Node>>`, narrow it to the current role, and call the consensus operations.

- **`rafty/src/node_state.rs`** — `NodeId`, the public `Node` union (`Follower`, `Candidate`, `Leader`), and `State<S>`, a typed-state model over the sealed `Follower`, `Candidate`, and `Leader` markers. Each state exposes only its valid operations; `Node` is narrowed with `try_follower` / `try_candidate` / `try_leader`. `State::<Follower>::new(...)` is the only constructor; other states are reached via transitions. `State` shares identity, term (`Arc<AtomicU64>`), cluster client, log, storage, and job registry across transitions.
- **`rafty/src/job/`** — `registry` contains the `Job` trait, `JobRegistry`, `JobKey`, `Trigger`, and `Control`. `election_countdown`, `heartbeat_polling`, and `log_replication` contain the three built-in jobs. The registry owns the driver task, timers, and cancellation; jobs only execute one bounded unit of work.
- **`rafty/src/remote_node.rs`** — `RemoteNode`, `ClusterClient`, and the role-specific transport traits `CandidateClient`, `FollowerClient`, and `LeaderClient`. `ClusterClient` broadcasts requests, applies per-call timeouts, and checks quorum. The peer map is expected to exclude the local node.
- **`rafty/src/transport/model.rs`** — `ClusterMessage<T>` / `Metadata`, `NodeAware`, `Key`, and the command, heartbeat, commit, log-replication, and vote message types.
- **`rafty/src/log.rs`** — `CommandLog`, the timestamp-cursored replicated command log trait. `InMemoryCommandLog` is a stub whose methods call `todo!()`.
- **`rafty/src/storage.rs`** — `Storage`, the materialized key-value state trait. `InMemoryStorage` is a stub whose methods call `todo!()`.
- **`rafty/tests/`** — integration tests plus `support/mod.rs`, which provides the in-memory `FakeLog`, `FakeStorage`, transport recorders, and a scripted follower. Use these fakes when adding tests.

Status: early/WIP, library only. No binary, HTTP server, network transport, config loader, persistence, snapshots, or membership-change protocol exists in this checkout. The log uses `DateTime<Utc>` cursors rather than Raft `(index, term)` entries, so log matching, commit index, conflict resolution, and truncation are not implemented. `Leader -> Follower` step-down is explicit, the election countdown is not reset by `process_heartbeat`, and role changes do not start/stop jobs automatically. See `rafty/README.md` for the full component and limitation overview.

## Commands

- `cargo check --workspace --all-targets` — fastest structural check after changes.
- `cargo test -p rafty` — run the library and integration tests.
- `cargo test --workspace --all-targets` — what CI runs.
- `cargo fmt --all --check` — formatting check (CI enforces it).
- `cargo build --locked --workspace --all-targets` — CI build.

## Conventions

- Consensus and state-transition logic belongs in `rafty`. Do not add HTTP handlers, DI wiring, or concrete transport implementations to this crate; if a server crate is introduced, keep that boundary.
- A `Job` must not loop forever waiting for events and must not spawn detached tasks. Return `Control::Continue` or `Control::Done` from one bounded `execute`, and let `JobRegistry` own timing and cancellation.
- Do not hold an `ArcSwap` guard across an `await` when doing network work; clone the typed `State` out first (as the tests do). Update shared `Node` state with `compare_and_swap` rather than mutating in place; `Node` has no interior mutability.
- `JobKey` is a closed enum. A new job kind currently requires a new variant or has to reuse an existing key, which prevents it from running alongside that built-in job.
- When changing public modules, state transitions, job semantics, or supported commands, update `rafty/README.md` and this file together.
