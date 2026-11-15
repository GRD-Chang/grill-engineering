"""One Run-owned relational schema per managed repository, no migration reader."""
from functools import lru_cache

PROTOCOL = "independent-delivery-repository-v23"

REPOSITORY_RUN_SCHEMA = """
CREATE TABLE meta (protocol TEXT NOT NULL);
CREATE TABLE runs (
 id TEXT NOT NULL,
 repository TEXT NOT NULL,
 parent_issue INTEGER NOT NULL,
 parent_revision TEXT NOT NULL,
 graph_revision TEXT NOT NULL,
 accepted_parent_revision TEXT NOT NULL,
 concurrency INTEGER NOT NULL CHECK(concurrency>0),
 policy BLOB NOT NULL,
 executor TEXT,
 epoch INTEGER NOT NULL DEFAULT 0,
 stopped INTEGER NOT NULL DEFAULT 0,
 final_action TEXT,
 integration_holder TEXT,
 next_ready INTEGER NOT NULL DEFAULT 0,
 blocked TEXT,
 source_sequence INTEGER,
 observed_parent_revision TEXT,
 observed_graph_revision TEXT,
 language TEXT CHECK(language IN ('zh','en')),
 repository_root TEXT,
 created_at REAL NOT NULL,
 profile_revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(id),
 FOREIGN KEY(id,profile_revision) REFERENCES profile_revisions(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(id,final_action) REFERENCES actions(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(id,integration_holder) REFERENCES subjects(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(id,source_sequence) REFERENCES source_observations(run_id,sequence) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE subjects (
 id TEXT NOT NULL,
 run_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 issue INTEGER NOT NULL,
 revision TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 1,
 version INTEGER NOT NULL DEFAULT 1,
 phase TEXT NOT NULL,
 purpose TEXT NOT NULL,
 source_ref TEXT NOT NULL,
 target_ref TEXT NOT NULL,
 base_sha TEXT NOT NULL,
 base_tree TEXT NOT NULL,
 source_sha TEXT NOT NULL,
 candidate_sha TEXT,
 candidate_tree TEXT,
 integrated_sha TEXT,
 integrated_tree TEXT,
 expected_tree TEXT,
 integration_target TEXT,
 completion_revision TEXT NOT NULL,
 current_attempt TEXT,
 review_attempt TEXT,
 publication_attempt TEXT,
 publication_sha TEXT,
 publication_tree TEXT,
 pr_number INTEGER,
 checks_id TEXT,
 grant_id TEXT,
 merge_sha TEXT,
 gate_mode TEXT,
 development_thread TEXT,
 ci_failure TEXT,
 last_failure TEXT,
 checkout TEXT NOT NULL DEFAULT '',
 workspace_sha TEXT,
 resume_purpose TEXT,
 repair_source TEXT,
 validation_checkout TEXT,
 validation_cleanup TEXT,
 owned_root TEXT NOT NULL DEFAULT '',
 common_git_dir TEXT NOT NULL DEFAULT '',
 refresh_revision TEXT,
 policy_id TEXT,
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,policy_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,current_attempt) REFERENCES attempts(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,review_attempt) REFERENCES attempts(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,publication_attempt) REFERENCES attempts(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,checks_id) REFERENCES checks(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,grant_id) REFERENCES grants(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,development_thread) REFERENCES threads(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,ci_failure) REFERENCES material(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,last_failure) REFERENCES material(run_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE run_repairs (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 subject TEXT NOT NULL,
 sequence INTEGER NOT NULL,
 generation INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','merged')),
 source_ref TEXT NOT NULL,
 target_ref TEXT NOT NULL,
 run_sha TEXT NOT NULL,
 default_sha TEXT NOT NULL,
 candidate_sha TEXT NOT NULL,
 candidate_tree TEXT NOT NULL,
 integrated_sha TEXT NOT NULL,
 integrated_tree TEXT NOT NULL,
 expected_tree TEXT NOT NULL,
 review_attempt TEXT NOT NULL,
 revision TEXT NOT NULL,
 parent_revision TEXT NOT NULL,
 graph_revision TEXT NOT NULL,
 completion_revision TEXT NOT NULL,
 published_sha TEXT,
 pr_number INTEGER,
 checks_id TEXT,
 merge_sha TEXT,
 PRIMARY KEY(run_id,id),
 UNIQUE(run_id,subject,sequence),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,review_attempt) REFERENCES attempts(run_id,id),
 FOREIGN KEY(run_id,checks_id) REFERENCES checks(run_id,id)
);
CREATE UNIQUE INDEX current_run_repair ON run_repairs(run_id,subject) WHERE state='open';
CREATE TABLE dependencies (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 predecessor TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,predecessor) REFERENCES subjects(run_id,id),
 PRIMARY KEY(run_id,subject,predecessor)
);
CREATE TABLE requirement_versions (
 run_id TEXT NOT NULL,
 issue INTEGER NOT NULL,
 revision TEXT NOT NULL,
 title TEXT NOT NULL,
 body TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,issue,revision)
);
CREATE TABLE issue_facts (
 run_id TEXT NOT NULL,
 issue INTEGER NOT NULL,
 revision TEXT NOT NULL,
 state TEXT NOT NULL,
 labels BLOB NOT NULL,
 position INTEGER NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,issue)
);
CREATE TABLE dependency_facts (
 run_id TEXT NOT NULL,
 issue INTEGER NOT NULL,
 blocker INTEGER NOT NULL,
 state TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,issue,blocker)
);
CREATE TABLE source_observations (
 run_id TEXT NOT NULL,
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 parent_revision TEXT NOT NULL,
 graph_revision TEXT NOT NULL,
 body BLOB NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 UNIQUE(run_id,sequence)
);
CREATE TABLE final_target (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 source_ref TEXT NOT NULL,
 target_ref TEXT NOT NULL,
 checkout TEXT NOT NULL,
 owned_root TEXT NOT NULL,
 common_git_dir TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id)
);
CREATE TABLE completions (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 generation INTEGER NOT NULL,
 revision TEXT NOT NULL,
 parent_revision TEXT NOT NULL,
 graph_revision TEXT NOT NULL,
 source_sha TEXT NOT NULL,
 merge_sha TEXT NOT NULL,
 tree TEXT NOT NULL,
 pr_number INTEGER NOT NULL,
 gate_mode TEXT NOT NULL,
 review_attempt TEXT,
 development_attempt TEXT,
 comment_effect TEXT NOT NULL,
 close_effect TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,comment_effect) REFERENCES effects(run_id,id),
 FOREIGN KEY(run_id,close_effect) REFERENCES effects(run_id,id),
 PRIMARY KEY(run_id,subject,generation),
 FOREIGN KEY(run_id,review_attempt) REFERENCES attempts(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,development_attempt) REFERENCES attempts(run_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE final_barriers (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 generation INTEGER NOT NULL,
 completion_revision TEXT NOT NULL,
 run_sha TEXT NOT NULL,
 default_sha TEXT NOT NULL,
 evidence TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,evidence) REFERENCES material(run_id,id),
 PRIMARY KEY(run_id,subject,generation)
);
CREATE TABLE retired_generations (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 generation INTEGER NOT NULL,
 revision TEXT NOT NULL,
 source_ref TEXT NOT NULL,
 checkout TEXT NOT NULL,
 source_sha TEXT NOT NULL,
 candidate_sha TEXT,
 publication_sha TEXT,
 pr_number INTEGER,
 reason TEXT NOT NULL,
 validation_checkout TEXT,
 validation_cleanup TEXT,
 workspace_sha TEXT NOT NULL,
 owned_root TEXT NOT NULL,
 common_git_dir TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 PRIMARY KEY(run_id,subject,generation)
);
CREATE TABLE resource_cleanup (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 generation INTEGER NOT NULL,
 source_ref TEXT NOT NULL,
 source_sha TEXT NOT NULL,
 checkout TEXT NOT NULL,
 workspace_sha TEXT NOT NULL,
 owned_root TEXT NOT NULL,
 common_git_dir TEXT NOT NULL,
 stage INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'pending',
 effect_id TEXT,
 outcome TEXT,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,effect_id) REFERENCES effects(run_id,id),
 PRIMARY KEY(run_id,subject,generation)
);
CREATE TABLE issue_links (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 generation INTEGER NOT NULL,
 source_ref TEXT NOT NULL,
 source_sha TEXT NOT NULL,
 issue INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',
 effect_id TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 PRIMARY KEY(run_id,subject),
 FOREIGN KEY(run_id,effect_id) REFERENCES effects(run_id,id)
);
CREATE TABLE budgets (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 purpose TEXT NOT NULL,
 window INTEGER NOT NULL,
 developments_used INTEGER NOT NULL DEFAULT 0,
 reviews_used INTEGER NOT NULL DEFAULT 0,
 development_limit INTEGER NOT NULL,
 review_limit INTEGER,
 review_required INTEGER NOT NULL,
 current INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 PRIMARY KEY(run_id,subject,purpose,window)
);
CREATE UNIQUE INDEX current_budget ON budgets(run_id,subject,purpose) WHERE current=1;
CREATE TABLE resync_allowances (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 generation INTEGER NOT NULL,
 used INTEGER NOT NULL DEFAULT 0,
 allowance INTEGER NOT NULL CHECK(allowance>=0),
 current INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 PRIMARY KEY(run_id,subject,generation)
);
CREATE UNIQUE INDEX current_resync ON resync_allowances(run_id,subject) WHERE current=1;
CREATE TABLE integration_snapshots (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 window INTEGER NOT NULL,
 allowance_generation INTEGER NOT NULL,
 source_ref TEXT NOT NULL,
 target_ref TEXT NOT NULL,
 source_sha TEXT NOT NULL,
 target_sha TEXT NOT NULL,
 open INTEGER NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 PRIMARY KEY(run_id,subject,window),
 FOREIGN KEY(run_id,subject,allowance_generation) REFERENCES resync_allowances(run_id,subject,generation)
);
CREATE TABLE material (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 subject TEXT NOT NULL,
 purpose TEXT NOT NULL,
 digest TEXT NOT NULL,
 size INTEGER NOT NULL,
 body BLOB NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 UNIQUE(run_id,subject,purpose,digest)
);
CREATE TABLE threads (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 subject TEXT NOT NULL,
 role TEXT NOT NULL,
 profile_revision INTEGER NOT NULL,
 backend_id TEXT UNIQUE,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,profile_revision) REFERENCES profile_revisions(run_id,id)
);
CREATE TABLE profile_revisions (
 run_id TEXT NOT NULL,
 id INTEGER NOT NULL,
 subject TEXT NOT NULL,
 material_id TEXT NOT NULL,
 created_at REAL NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,material_id) REFERENCES material(run_id,id)
);
CREATE TABLE run_inputs (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 material_id TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,material_id) REFERENCES material(run_id,id)
);
CREATE TABLE attempts (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 subject TEXT NOT NULL,
 generation INTEGER NOT NULL,
 ordinal INTEGER NOT NULL,
 role TEXT NOT NULL,
 purpose TEXT NOT NULL,
 window INTEGER NOT NULL,
 parent_revision TEXT NOT NULL,
 graph_revision TEXT NOT NULL,
 revision TEXT NOT NULL,
 base_sha TEXT NOT NULL,
 candidate_sha TEXT,
 candidate_tree TEXT,
 expected_tree TEXT,
 completion_revision TEXT NOT NULL,
 context_id TEXT NOT NULL,
 current_invocation TEXT,
 result_id TEXT,
 status TEXT NOT NULL,
 accounted INTEGER NOT NULL DEFAULT 0,
 outcome TEXT,
 new_thread INTEGER NOT NULL DEFAULT 0,
 resume_action TEXT,
 thread_id TEXT,
 opening_id TEXT,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,context_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,result_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,resume_action) REFERENCES actions(run_id,id),
 FOREIGN KEY(run_id,thread_id) REFERENCES threads(run_id,id),
 FOREIGN KEY(run_id,opening_id) REFERENCES material(run_id,id),
 UNIQUE(run_id,subject,ordinal),
 FOREIGN KEY(run_id,current_invocation) REFERENCES invocations(run_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE invocations (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 attempt TEXT NOT NULL,
 thread TEXT NOT NULL,
 executor TEXT NOT NULL,
 epoch INTEGER NOT NULL,
 status TEXT NOT NULL,
 allocated_at REAL NOT NULL,
 deadline_seconds REAL NOT NULL,
 deadline_at REAL NOT NULL,
 predecessor TEXT,
 recovery_kind TEXT CHECK(recovery_kind IN ('ordinary','capacity','explicit','semantic')),
 action_id TEXT,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,attempt) REFERENCES attempts(run_id,id),
 FOREIGN KEY(run_id,thread) REFERENCES threads(run_id,id),
 FOREIGN KEY(run_id,predecessor) REFERENCES invocations(run_id,id),
 FOREIGN KEY(run_id,action_id) REFERENCES actions(run_id,id)
);
CREATE TABLE invocation_outputs (
 run_id TEXT NOT NULL,
 invocation TEXT NOT NULL,
 ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 2),
 context_id TEXT NOT NULL,
 lease TEXT NOT NULL UNIQUE,
 raw_id TEXT,
 dispatch_state TEXT NOT NULL DEFAULT 'allocated',
 process_identity TEXT,
 exit_code INTEGER,
 start_evidence TEXT,
 allocated_at REAL NOT NULL,
 dispatched_at REAL,
 started_at REAL,
 returned_at REAL,
 exited_at REAL,
 mode TEXT NOT NULL,
 requested_thread TEXT,
 failure_kind TEXT CHECK(failure_kind IN ('ordinary','capacity','fatal')),
 recovery_ready_at REAL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,invocation) REFERENCES invocations(run_id,id),
 FOREIGN KEY(run_id,context_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,raw_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,process_identity) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,start_evidence) REFERENCES material(run_id,id),
 PRIMARY KEY(run_id,invocation,ordinal)
);
CREATE TABLE worker_activity (
 run_id TEXT NOT NULL,
 invocation TEXT NOT NULL,
 ordinal INTEGER NOT NULL,
 identity TEXT NOT NULL,
 active INTEGER NOT NULL,
 observed_at REAL NOT NULL,
 evidence TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,evidence) REFERENCES material(run_id,id),
 PRIMARY KEY(run_id,invocation,ordinal,identity),
 FOREIGN KEY(run_id,invocation,ordinal) REFERENCES invocation_outputs(run_id,invocation,ordinal)
);
CREATE TABLE work_queue (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 ready INTEGER NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 PRIMARY KEY(run_id,subject),
 UNIQUE(run_id,ready)
);
CREATE TABLE integration_queue (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 ready INTEGER NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 PRIMARY KEY(run_id,subject),
 UNIQUE(run_id,ready)
);
CREATE TABLE leases (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 subject TEXT NOT NULL,
 invocation TEXT NOT NULL,
 executor TEXT NOT NULL,
 epoch INTEGER NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 UNIQUE(run_id,subject),
 FOREIGN KEY(run_id,invocation) REFERENCES invocations(run_id,id),
 UNIQUE(run_id,invocation)
);
CREATE TABLE retired_executors (
 run_id TEXT NOT NULL,
 owner TEXT NOT NULL,
 epoch INTEGER NOT NULL,
 status TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,owner,epoch)
);
CREATE TABLE hosts (
 run_id TEXT NOT NULL,
 epoch INTEGER NOT NULL,
 owner TEXT NOT NULL,
 nonce TEXT NOT NULL UNIQUE,
 action_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 request_id TEXT NOT NULL,
 unit_name TEXT UNIQUE,
 backend TEXT NOT NULL CHECK(backend IN ('systemd','foreground')),
 domain_id TEXT,
 allocated_at REAL NOT NULL,
 dispatched_at REAL,
 status TEXT NOT NULL CHECK(status IN ('allocated','dispatched','running','unknown','exited')),
 invocation_id TEXT,
 cgroup TEXT,
 boot_id TEXT,
 main_pid INTEGER,
 start_token TEXT,
 exit_status INTEGER,
 ended_at REAL,
 observation_id TEXT,
 usage_ready INTEGER NOT NULL DEFAULT 0,
 usage_released INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,action_id) REFERENCES actions(run_id,id),
 UNIQUE(run_id,action_id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,request_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,domain_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,observation_id) REFERENCES material(run_id,id),
 PRIMARY KEY(run_id,epoch),
 UNIQUE(run_id,owner)
);
CREATE TABLE waits (
 run_id TEXT NOT NULL,
 subject TEXT,
 kind TEXT NOT NULL,
 scope_id TEXT NOT NULL,
 started_at REAL NOT NULL,
 deadline REAL NOT NULL,
 retry_count INTEGER NOT NULL DEFAULT 0,
 next_retry_at REAL,
 evidence TEXT,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,scope_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,evidence) REFERENCES material(run_id,id),
 PRIMARY KEY(run_id,scope_id,kind)
);
CREATE TABLE gates (
 run_id TEXT NOT NULL,
 subject TEXT NOT NULL,
 code TEXT NOT NULL,
 purpose TEXT,
 evidence TEXT,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,evidence) REFERENCES material(run_id,id),
 PRIMARY KEY(run_id,subject,code)
);
CREATE TABLE effects (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 subject TEXT NOT NULL,
 operation TEXT NOT NULL,
 status TEXT NOT NULL,
 executor TEXT,
 epoch INTEGER,
 parent_revision TEXT NOT NULL,
 graph_revision TEXT NOT NULL,
 revision TEXT NOT NULL,
 generation INTEGER NOT NULL,
 source_ref TEXT NOT NULL,
 target_ref TEXT NOT NULL,
 before_sha TEXT NOT NULL,
 after_sha TEXT,
 candidate_sha TEXT,
 candidate_tree TEXT,
 base_sha TEXT NOT NULL,
 base_tree TEXT NOT NULL,
 pr_number INTEGER,
 actor TEXT CHECK(actor IS NULL OR length(trim(actor))>0),
 payload_id TEXT NOT NULL,
 receipt_id TEXT,
 attempts INTEGER NOT NULL DEFAULT 0,
 retry_baseline INTEGER NOT NULL DEFAULT 0,
 control_action TEXT,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,payload_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,receipt_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,control_action) REFERENCES actions(run_id,id),
 CHECK((executor IS NOT NULL AND epoch IS NOT NULL) OR (status='confirmed' AND executor IS NULL AND epoch IS NULL)),
 CHECK(actor IS NOT NULL OR status='confirmed' OR operation NOT IN ('push','run_branch','pr_write','merge','completion_comment','close','issue_link','cleanup_remote_ref','retire_pr','reopen_ticket'))
);
CREATE INDEX control_effects ON effects(run_id,control_action);
CREATE TABLE effect_failures (
 run_id TEXT NOT NULL,
 effect TEXT NOT NULL,
 dispatch_serial INTEGER NOT NULL,
 evidence TEXT NOT NULL,
 observed_at REAL NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,effect) REFERENCES effects(run_id,id),
 FOREIGN KEY(run_id,evidence) REFERENCES material(run_id,id),
 PRIMARY KEY(run_id,effect,dispatch_serial)
);
CREATE UNIQUE INDEX pending_effect ON effects(run_id,subject) WHERE status IN ('prepared','sent','observed') AND operation NOT IN ('cleanup_worktree','cleanup_local_ref','cleanup_remote_ref','issue_link');
CREATE TABLE checks (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 subject TEXT NOT NULL,
 pr_number INTEGER NOT NULL,
 head_sha TEXT NOT NULL,
 result TEXT NOT NULL,
 material_id TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,material_id) REFERENCES material(run_id,id)
);
CREATE TABLE grants (
 run_id TEXT NOT NULL,
 id TEXT NOT NULL,
 subject TEXT NOT NULL,
 action TEXT NOT NULL,
 pr_number INTEGER NOT NULL,
 head_sha TEXT NOT NULL,
 base_sha TEXT NOT NULL,
 review_attempt TEXT NOT NULL,
 checks_id TEXT NOT NULL,
 revision TEXT NOT NULL,
 parent_revision TEXT NOT NULL,
 graph_revision TEXT NOT NULL,
 completion_revision TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,action) REFERENCES actions(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,review_attempt) REFERENCES attempts(run_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(run_id,checks_id) REFERENCES checks(run_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE actions (
 id TEXT NOT NULL,
 run_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 subject TEXT,
 payload_id TEXT,
 status TEXT NOT NULL,
 receipt_id TEXT,
 PRIMARY KEY(run_id,id),
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,payload_id) REFERENCES material(run_id,id),
 FOREIGN KEY(run_id,receipt_id) REFERENCES material(run_id,id)
);
CREATE TABLE history (
 run_id TEXT NOT NULL,
 position INTEGER PRIMARY KEY AUTOINCREMENT,
 subject TEXT NOT NULL,
 kind TEXT NOT NULL,
 identity TEXT NOT NULL,
 material_id TEXT,
 generation INTEGER NOT NULL,
 created_at REAL NOT NULL,
 phase TEXT,
 purpose TEXT,
 status TEXT,
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(run_id,subject) REFERENCES subjects(run_id,id),
 FOREIGN KEY(run_id,material_id) REFERENCES material(run_id,id),
 UNIQUE(run_id,position)
);
CREATE INDEX history_action_facts ON history(run_id,kind,identity);

CREATE INDEX source_observation_run_page ON source_observations(run_id,sequence);
CREATE INDEX history_run_page ON history(run_id,position);
"""

# Additive Task layer of the SAME repository database. Earlier
# Run-domain fixtures intentionally use the Run-only foundation above; neither
# definition is selected by public Storage/CLI/Host until activation is complete.
REPOSITORY_TASK_SCHEMA = """
CREATE UNIQUE INDEX repository_run_task_identity
 ON runs(id,repository,repository_root,parent_issue);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY NOT NULL,
 repository TEXT NOT NULL,
 repository_root TEXT NOT NULL,
 parent_issue INTEGER NOT NULL CHECK(parent_issue>0),
 current_run TEXT,
 UNIQUE(repository,repository_root,parent_issue),
 UNIQUE(id,repository,repository_root,parent_issue),
 FOREIGN KEY(id,current_run) REFERENCES task_runs(task_id,run_id)
  DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE task_runs (
 task_id TEXT NOT NULL,
 run_id TEXT NOT NULL UNIQUE,
 repository TEXT NOT NULL,
 repository_root TEXT NOT NULL,
 parent_issue INTEGER NOT NULL,
 PRIMARY KEY(task_id,run_id),
 FOREIGN KEY(task_id,repository,repository_root,parent_issue)
  REFERENCES tasks(id,repository,repository_root,parent_issue),
 FOREIGN KEY(run_id,repository,repository_root,parent_issue)
  REFERENCES runs(id,repository,repository_root,parent_issue)
);
"""


REPOSITORY_ROUTING_SCHEMA = """
CREATE TABLE save_admissions (
 position INTEGER PRIMARY KEY AUTOINCREMENT,
 request TEXT NOT NULL UNIQUE CHECK(length(request)=32 AND request NOT GLOB '*[^0-9a-f]*'),
 run_id TEXT REFERENCES runs(id),
 clock_domain TEXT NOT NULL CHECK(length(clock_domain) BETWEEN 1 AND 512),
 deadline REAL NOT NULL CHECK(deadline>0 AND deadline<1.0e100)
);
CREATE TRIGGER save_admission_capacity BEFORE INSERT ON save_admissions
WHEN (SELECT count(*) FROM save_admissions)>=128
BEGIN SELECT RAISE(ABORT,'save admission capacity'); END;
CREATE TRIGGER save_admission_immutable BEFORE UPDATE ON save_admissions
BEGIN SELECT RAISE(ABORT,'save registration is immutable'); END;
CREATE TABLE repository_binding (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 repository TEXT NOT NULL,
 repository_root TEXT NOT NULL
);
CREATE TRIGGER repository_binding_single_insert BEFORE INSERT ON repository_binding
WHEN EXISTS (SELECT 1 FROM repository_binding)
BEGIN SELECT RAISE(ABORT,'repository binding is immutable'); END;
CREATE TRIGGER repository_binding_immutable_update BEFORE UPDATE ON repository_binding
BEGIN SELECT RAISE(ABORT,'repository binding is immutable'); END;
CREATE TRIGGER repository_binding_immutable_delete BEFORE DELETE ON repository_binding
BEGIN SELECT RAISE(ABORT,'repository binding is immutable'); END;
CREATE TRIGGER repository_run_insert BEFORE INSERT ON runs
WHEN NOT EXISTS (SELECT 1 FROM repository_binding WHERE singleton=1 AND repository=NEW.repository AND repository_root=NEW.repository_root)
BEGIN SELECT RAISE(ABORT,'repository namespace mismatch'); END;
CREATE TRIGGER repository_run_update BEFORE UPDATE OF repository,repository_root ON runs
WHEN NOT EXISTS (SELECT 1 FROM repository_binding WHERE singleton=1 AND repository=NEW.repository AND repository_root=NEW.repository_root)
BEGIN SELECT RAISE(ABORT,'repository namespace mismatch'); END;
CREATE TRIGGER repository_task_insert BEFORE INSERT ON tasks
WHEN NOT EXISTS (SELECT 1 FROM repository_binding WHERE singleton=1 AND repository=NEW.repository AND repository_root=NEW.repository_root)
BEGIN SELECT RAISE(ABORT,'repository namespace mismatch'); END;
CREATE TRIGGER repository_task_update BEFORE UPDATE OF repository,repository_root ON tasks
WHEN NOT EXISTS (SELECT 1 FROM repository_binding WHERE singleton=1 AND repository=NEW.repository AND repository_root=NEW.repository_root)
BEGIN SELECT RAISE(ABORT,'repository namespace mismatch'); END;
"""


def repository_schema_objects() -> dict[tuple[str, str], str]:
    """Exact owned DDL for protocol admission; parse only, never execute a model."""
    # Each caller owns its mutable view. The key contains all current source
    # declarations, never a database identity or an earlier admission result.
    return dict(_parse_repository_schema_objects(
        REPOSITORY_RUN_SCHEMA, REPOSITORY_TASK_SCHEMA, REPOSITORY_ROUTING_SCHEMA))


@lru_cache(maxsize=1)
def _parse_repository_schema_objects(run_schema: str, task_schema: str,
                                    routing_schema: str) -> tuple[tuple[tuple[str, str], str], ...]:
    import re
    import sqlite3
    result: dict[tuple[str, str], str] = {}
    statement = ""
    for line in (run_schema + task_schema + routing_schema).splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        sql = statement.strip().removesuffix(";").strip()
        match = re.fullmatch(r"CREATE (?:UNIQUE )?(TABLE|INDEX|TRIGGER) (\w+)[\s\S]*", sql)
        if match is None:
            raise RuntimeError("Unrecognized schema declaration")
        result[(match[1].lower(), match[2])] = sql
        statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete schema declaration")
    return tuple(result.items())
