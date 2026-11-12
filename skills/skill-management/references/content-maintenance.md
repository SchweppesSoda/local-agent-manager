# Content maintenance

Identify whether the requested Skill lives in a standalone directory, a source repository, or a managed package. Edit its authoritative source and follow that source's maintenance instructions. For a new Skill, use the user's selected location and client scope. Package-owned copies follow their owning update workflow. An existing standalone Skill does not need a deployment manifest or source registration for content maintenance.

Read the current source and relevant references before editing. Protect existing changes. Keep descriptions focused on the action and trigger. Put long workflows in references and preserve necessary precision and authorization boundaries. Keep UI metadata consistent with the Skill's actual scope.

Validate frontmatter, relative resources, and representative positive and negative routing cases. Use `local-agent-manager skills audit --repo-root <source>` for a requested source audit. Static `sourceAudit.sharedResourceReferences` may permit exact cross-Skill resources within that source. Shared resources remain owned by their original Skill.

Dependency changes require the source's documented runtime checks. Pure prose changes need a diff and affected-reference review. Existing direct mounts reflect source edits; copied or packaged deployments need their own update path. Client refresh follows that client's supported behavior, and a source edit alone does not prove that a client loaded the new body.
