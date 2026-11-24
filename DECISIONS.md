# Decisions

- Use version 1 for action, observation, run and model configuration payloads.
- Keep the bridge on IPv4 loopback. Use polling for run updates.
- Keep the model optional. Accept Studio JSON settings and two documented HTTP response shapes.
- Require concrete assertions per step. Retry an unresolved selector only when screen evidence supports the same intended element.
- Store screenshots separately from run results and omit screenshots when password nodes are present or report screenshots are disabled.
