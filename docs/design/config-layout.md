# Config Layout

Cymphony uses a split config layout:

- `.cymphony/config.yml`: runtime settings
- `.cymphony/prompts/execution.md`: implementation-agent prompt
- `.cymphony/prompts/qa_review.md`: QA-review prompt

Resolution order:

1. `--workflow-path <path>`
2. `.cymphony/config.yml`
3. setup mode

Committed examples live at:

- `config.example.yml`
- `prompts.example/execution.md`
- `prompts.example/qa_review.md`
