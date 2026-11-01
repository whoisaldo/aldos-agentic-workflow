---
name: job-apply
description: Tailor the user's resume using their most relevant current experience and major projects, fill the application as a draft, and create screenshots in a downloadable HTML review file. Use for the user's own job applications and explicitly approved submissions.
---

# Job applications

Prepare a truthful, tailored application that the user can inspect remotely. Never submit an application without the user's explicit approval for that specific application. "Apply", "continue", and approval for another job do not authorize submission.

Read [the user's profile](references/profile.md) before filling personal or disclosure fields. Use the owner-supplied answers there without asking again. Current user instructions and verified changes take precedence. If the profile is still a blank template, ask the user to fill it in privately before starting.

## Resume and supporting materials

- Start from the most relevant current resume or tailored cut, together with verified portfolio and project evidence. Choose by relevance, freshness, and impact; do not make finding a canonical repository the first priority or a prerequisite. A resume workspace named in the profile is an available source, not a mandatory starting file. Read the applicable AGENTS.md when working in a repository. Use the `resume-craft` skill for tailoring when available.
- Read the live posting, record its URL and requisition, and save its requirements with the cut. Tailor the selection, order, and wording of verified accomplishments to the actual job.
- Follow the experience ordering and the major-project and employer priorities in [the profile](references/profile.md). If it names a lead employer, put that employer first in the resume's Experience section. Tailor the evidence and bullet emphasis within that strong material instead of resurrecting minor older projects for keyword overlap.
- Verify new project or framework claims from the owner's contributions, source, merged PRs, measurements, or supplied evidence. Distinguish integration from authorship and synthetic benchmarks from hardware results. Do not invent metrics, dates, skills, or qualifications.
- Save every new tailored resume in the resume repository named in the profile, at `tailored/<employer-role-or-requisition>/`, with its source, built PDF, and provenance notes. Choosing the most relevant source material and saving the finished cut in this repository are separate decisions. Preserve submitted PDFs and review records as evidence of what was sent.
- Repository source and PDF filenames may include the employer or role, for example `tailored/acme-ml-platform/First_Last_Resume_Acme.tex` and its built PDF. Keep that descriptive repository copy. For the application itself, stage a separate byte-identical upload copy with the neutral basename from the profile, for example `First_Last_Resume.pdf`. Only the uploaded filename must omit employer names, requisition IDs, and tailoring labels. Verify that the upload copy matches the built PDF exactly. Never print or re-export the PDF to rename it.
- Build using the applicable checks, inspect the PDF, and verify the neutral filename shown in the portal before submission. Follow the resume workspace's own page limit and build command after every resume edit.
- Follow the profile's GPA policy. An explicitly mandatory GPA field needs the verified current value. Never invent or round a value to meet a preference.
- Follow the profile's resume/CV preference. If the user has only a resume, do not create or request a CV as part of this workflow, and upload the tailored resume to a generic "Resume/CV" field. Create other supporting materials only when actually required or requested.
- Include the user's portfolio and code-hosting profile from the profile in application website fields, and LinkedIn in its own field when present. Do not expose private repository URLs.

## Fill and verify the saved draft

Use the available collaborative browser, preferably T3 preview tools. Start with browser status, inspect the actual page, and use current semantic locators. Preserve the user's control of human-owned tabs.

Reuse the existing account for the application email. Keep passwords, activation links, and tokens out of skills, source control, screenshots, and review bundles. Use private secret input where available. Do not create duplicate accounts to work around sign-in errors.

Fill from the resume, profile, and evidence already available. Resolve routine formatting and equivalent choices without repeated questions. Ask only for information that is genuinely missing and cannot safely be inferred. Do not guess sensitive, legal, or factual answers.

Check parsed employment, titles, dates, education, current-role flags, descriptions, skills, uploaded filename, URLs, and authorization answers. Projects parsed as jobs and combined co-op/return dates often need correction.

Apply the profile's lead-employer ordering to manually ordered employment entries as well. If the portal enforces its own ordering, keep accurate dates and report that limitation rather than falsifying chronology to force an order.

For repeating groups, add one row at a time and wait for its new input before typing. Save and verify the persisted result. A local Review screen can omit stale server records. After changes to repeated entries, reload the saved draft and check actual row counts and distinct values before continuing. Remove duplicates and verify the deletion persisted. Read [portal notes](references/portals.md) for the observed Workday failure mode.

## Required review deliverables

Before requesting submission approval:

1. Reach the last review step and verify the complete application, not only the current viewport.
2. Expand collapsed information and save overlapping screenshots that cover every section and the attached resume. Exclude credentials and unrelated account information.
3. Save the final review text, tailored build PDF, screenshots, and a short status note identifying unresolved omissions or portal-specific mappings.
4. Create a self-contained HTML gallery with every screenshot embedded, including the resume preview. Use [the gallery helper](scripts/review_gallery.py) with a JSON manifest. Clicking a screenshot must show its full size. The HTML must contain no controls that can submit the live application.
5. If the user is working from another device, serve the gallery from a free port on a private network address that device can reach, such as a Tailscale address, and give them a working URL. Also provide the single HTML file for download. Do not assume a GUI opened on the agent's machine is visible to the user, and do not publish personal application data to a public site.
6. Save the self-contained HTML in the resume repository under the job's `tailored/<employer-role-or-requisition>/application-review.html`, outside ignored `build/` paths. Keep an `application.md` beside it with the role, requisition, application status, the working review URL, a relative link to the HTML, the exact uploaded resume filename and its SHA-256, and confirmation evidence when available. Update the repository's `applications.md` index with both review links and the accurate status. A server URL alone is not a durable record.

Put the clickable review URL in the final text reply as well as any inline visual, so the user can open the full screenshot gallery directly from their own device.

Example helper invocation:

```bash
python3 scripts/review_gallery.py --manifest /path/to/review-manifest.json --output /path/to/application-review.html
```

The manifest has `title`, `role`, `status` as `draft` or `submitted`, `note`, and `screenshots` containing `path`, `title`, and optional `alt`. Paths are relative to the manifest. Optional `links` contain `label`, an HTTP(S) `href`, and optional `download: true`. The helper embeds all images and needs only Python's standard library. Verify the served file, image loading, and full-size viewing before handoff. Keep application status accurate.

## Submission boundary

Stop with the completed review package until the user explicitly approves submission. A direct "submit" in context of the reviewed application is sufficient. Do not ask again when that approval is already present.

Use approval only for that application and the changes the user authorized with it. Do not carry it into future jobs, substantially different application content, or additional external messages.

After approval, click the final submission control once and wait for a portal confirmation or submitted application status. If the result is uncertain, inspect the existing application status before retrying. Never create a duplicate submission to resolve an ambiguous response.

Capture the confirmation, record the requisition/status, and update the HTML and status note to Submitted only when the portal confirms success. Save the updated HTML, confirmation evidence, and post-submission review URL in the job's repository folder and update `applications.md` before reporting completion. Keep drafts distinct from confirmed applications. Report a failed or uncertain submission accurately.
