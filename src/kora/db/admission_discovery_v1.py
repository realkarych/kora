INDEX_NAME = "ix_node_instance_initial_admission"
CANDIDATE_ROWS = (
    "kind IN ('agent', 'command') AND status IN ('pending', 'ready', 'waiting') "
    "AND top_level = true AND subgraph_attempt = 1 AND attempt_count = 0 "
    "AND current_attempt_id IS NULL"
)
