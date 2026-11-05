# Bundled workflow skills

These are pinned copies of the user-requested packages. They are read by this
harness's native skill runtime; Claude Code and Codex are not needed to load them.

| Package | Source | Pinned commit | License |
|---|---|---|---|
| Harness.io platform skills (56) | https://github.com/harness/harness-skills | `320f7d0e5f8f0cec4967276e44a6fa94eaab8c7a` | Apache-2.0 |
| WalkingLabs harness-creator (1) | https://github.com/walkinglabs/learn-harness-engineering | `38ddcd2bf8d65271f668b94e7c875ca1d629d622` | MIT |

WalkingLabs' guide: https://walkinglabs.github.io/learn-harness-engineering/en/skills/
Each vendored package includes its license and `PROVENANCE.json`. No upstream
scripts are run on startup, activation, tests or export. References and templates
retain their upstream layout so relative resource paths work.

Use `/skills`, `/skill harness-creator`, `/unskill harness-creator` in terminal.
API: `GET /v1/skills`, `GET /v1/skills/<name>?resource=references/file.md`;
activate with `skills: ["harness-creator"]` on a native run or session PATCH.

Metadata is discoverable without loading every skill body into model context.
Only explicitly activated bodies enter instructions. `read_skill` lets the model
load a selected resource as needed. All resource paths resolve inside the package.

The Harness.io skills implement CI/CD workflows for an external service, rather
than generic coding-agent behavior. Activation requires a configured
`harness-mcp-v2` MCP server with exposed tools. Enabling the skill does not grant
additional tools or credentials. Configure the server using the guide in
`docs/advanced-harness.md`; live platform use also needs scope
and the confirmations required by those skills.

To add a project skill, create `.harness/skills/<name>/SKILL.md` with YAML
frontmatter `name` and `description`, then instructions below the closing `---`.
Optional `references/` and `templates/` are loaded on demand. Use `/skills reload`
after changes. `HARNESS_SKILL_PATHS` overrides search roots (semicolon-separated
on Windows). Project-installed skills and state are excluded from public export.
