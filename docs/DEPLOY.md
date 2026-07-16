# Deploying Testra to AWS

One EC2 instance, two containers, one EBS volume. Push to `master` → CI builds
images, pushes them to ECR, and rolls the box. Roughly 30 minutes of one-time
setup, all of it in your AWS account.

```
             ┌──────── GitHub Actions (.github/workflows/ci.yml) ────────┐
push master →│ lint + pytest + build → ECR push → SSM roll → /health gate│
             └────────────────────────┬─────────────────────────────────┘
                                      │ OIDC (no stored AWS keys)
                                      ▼
        ┌───────────────────── EC2 instance ─────────────────────┐
        │  web (Caddy)  :80/:443  → SPA + TLS                    │
        │      └── /api/* ──► backend (FastAPI) :8000            │
        │                          └── TESTGEN_DB=/data/…        │
        └──────────────────────────┬─────────────────────────────┘
                                   ▼
                    EBS volume at /var/data  ← the only thing that matters
                      db/       SQLite: users, plans, Stripe mapping
                      caddy/    issued TLS certificates
                      testra.env  secrets (hand-written, never in CI)
```

## Why this shape

The app keeps all state in SQLite, which needs a real block device. That rules
out App Runner and Fargate (ephemeral filesystems — every deploy wipes the
database) and rules out EFS (SQLite's locking is not safe over NFS; the failure
mode is corruption, not an error). An EBS volume is just a disk, so `store.py`
needs no changes.

**The volume is the deployment.** Instances, images and containers here are all
disposable; `/var/data` is not. Keep that straight and nothing else is scary.

---

## 1. ECR — two repositories

```bash
aws ecr create-repository --repository-name testra-backend
aws ecr create-repository --repository-name testra-web
```

**Then give both a lifecycle policy.** CI tags every image with its commit SHA
and never deletes anything, so without this the registry grows forever. ECR's
free tier is only 500MB of private storage — the backend image alone is a few
hundred MB, so a handful of deploys pass it and you start paying for old builds
nobody will ever pull.

Keep the last 10 (enough to roll back to any recent deploy):

```bash
cat > /tmp/ecr-lifecycle.json <<'EOF'
{
  "rules": [{
    "rulePriority": 1,
    "description": "Keep the 10 most recent images; expire the rest",
    "selection": {
      "tagStatus": "any",
      "countType": "imageCountMoreThan",
      "countNumber": 10
    },
    "action": { "type": "expire" }
  }]
}
EOF

for repo in testra-backend testra-web; do
  aws ecr put-lifecycle-policy \
    --repository-name "$repo" \
    --lifecycle-policy-text file:///tmp/ecr-lifecycle.json
done
```

Ten is a deliberate floor: rollback means redeploying an older SHA, and an image
that has been expired cannot be rolled back to.

## 2. OIDC — let GitHub assume a role without stored keys

Add GitHub as an identity provider once per account:

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com
```

Then create a role (`testra-github-deploy`) with this **trust policy**:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:Krishna417211/ai-test-generator:environment:staging"
      }
    }
  }]
}
```

> **The `sub` line is the one people get wrong**, in two ways.
>
> First: because the deploy job declares `environment: staging`, GitHub's token
> says `…:environment:staging` — *not* `…:ref:refs/heads/master`. Use the ref
> form and you get `AccessDenied … sts:AssumeRoleWithWebIdentity`. If you remove
> `environment:` from the job, this has to change to the ref form.
>
> Second: it must be the repo's **current** name. This repo was renamed
> (`testgen-ai` → `ai-test-generator`), and GitHub redirects the old name for
> clone and push — so everything *looks* fine while the OIDC token carries the
> new name and fails to match a policy pinned to the old one. If you rename the
> repo again, update this policy or deploys stop.
>
> Never loosen it to `repo:*` or `*`. That condition is the only thing stopping
> any GitHub repository on earth from assuming this role.

**Permissions policy** for that role (push images, run one command on one box):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    { "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": [
        "arn:aws:ecr:<REGION>:<ACCOUNT_ID>:repository/testra-backend",
        "arn:aws:ecr:<REGION>:<ACCOUNT_ID>:repository/testra-web"
      ]
    },
    { "Effect": "Allow", "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/<INSTANCE_ID>",
        "arn:aws:ssm:<REGION>::document/AWS-RunShellScript"
      ]
    },
    { "Effect": "Allow", "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"], "Resource": "*" }
  ]
}
```

## 3. The instance

- **AMI**: Amazon Linux 2023 (the SSM agent is preinstalled)
- **Type**: `t3.small` if you're paying anyway. On a free-tier account use
  `t3.micro` — `t3.small` is **not** free-tier eligible. 1GB is tight but works
  because the box only pulls images, never builds them; `bootstrap.sh` adds swap
  so a spike degrades instead of OOM-killing the API. Note the free EC2 tier
  lasts 12 months from account creation, then stops — check what your account
  actually has rather than assuming.
- **Root volume**: default 8–16GB, disposable.
- **Data volume**: a **separate** EBS volume (20GB gp3 is plenty), attached as
  `/dev/sdf`. It shows up inside as `/dev/nvme1n1`. Separate, so you can
  terminate and replace the instance without touching the database.
- **Elastic IP**: allocate and associate one. Without it the public IP changes on
  every stop/start, breaking DNS and the baked-in `SITE_URL`.
- **Security group**: inbound `80` and `443` from `0.0.0.0/0`. **No port 22** —
  use SSM Session Manager (`aws ssm start-session --target <id>`) and the box
  never exposes SSH at all.
- **Instance profile**: attach a role with the managed policies
  `AmazonSSMManagedInstanceCore` (lets CI run the deploy) and
  `AmazonEC2ContainerRegistryReadOnly` (lets it pull images).

Then, once, on the box:

```bash
aws ssm start-session --target <INSTANCE_ID>
sudo dnf -y install git
git clone https://github.com/Krishna417211/ai-test-generator.git /tmp/testra
sudo bash /tmp/testra/deploy/bootstrap.sh /dev/nvme1n1
```

Check `lsblk` first and pass the **data** volume, not the root one. The script
refuses to touch a mounted device and won't reformat a volume that already has a
filesystem, so a re-run is safe — but it does run `mkfs` on a genuinely blank
disk, so read the argument twice.

## 4. Secrets

`bootstrap.sh` writes a skeleton at `/var/data/testra.env` (mode 600). Fill it
in — the app runs without it but can't do anything useful.

These live **on the instance only**: not in git, not in the image
(see `backend/.dockerignore`), not in GitHub. The deploy pipeline never reads
them, so a leaked workflow token can't disclose them.

At minimum, for the app to function: one LLM provider key, `SESSION_SECRET`,
`FRONTEND_URL` / `BACKEND_URL` / `CORS_ORIGINS` set to your site origin, and SMTP
(in production an unconfigured relay fails closed — signup verification and login
OTP will refuse rather than silently skip).

## 5. Repository configuration

**Settings → Environments → `staging`** (create it; add reviewers here if you
want a human gate before deploys).

| Kind | Name | Example |
|---|---|---|
| Secret | `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::123456789012:role/testra-github-deploy` |
| Variable | `AWS_REGION` | `ap-south-1` |
| Variable | `EC2_INSTANCE_ID` | `i-0abc123def456` |
| Variable | `SITE_URL` | `https://testra.example.com` |
| Variable | `SITE_ADDRESS` | `testra.example.com` |

`SITE_URL` is inlined into the JS bundle at build time, so changing it requires a
rebuild, not a restart. `SITE_ADDRESS` is what Caddy binds: a hostname turns on
automatic HTTPS; `:80` keeps it plain HTTP.

**On HTTPS:** Let's Encrypt will not issue a certificate for an
`*.amazonaws.com` name, so the EC2 public DNS name cannot serve HTTPS. You need
a domain pointed at the Elastic IP. Until then, set `SITE_ADDRESS=:80` and
`SITE_URL=http://<elastic-ip>` and treat it as staging only.

## 6. Deploy

Merge to `master`. The `deploy` job runs only after lint, pytest (3.11/3.12/3.13)
and the frontend build are green, only on a push, and never from a pull request —
so a fork's PR can't reach AWS.

It ends by polling `SITE_URL/health` and fails the run if it never returns 200.

**There is no automatic rollback.** If a deploy goes bad, re-run the last good
commit's workflow from the Actions tab; images are tagged by SHA, so the old
build is still in ECR.

---

## Turning billing on

**Not yet, and not on this checklist by accident.** The Stripe wiring is built
and unit-tested, but:

1. **It has never seen a real Stripe event.** The tests use hand-written
   fixtures, so they prove the logic is self-consistent, not that the payload
   shapes are right. Verify with the Stripe CLI in test mode
   (`stripe listen --forward-to <site>/api/billing/webhook`) and a real test-mode
   checkout before trusting it.
2. **HTTPS must be real first.** Stripe won't deliver live webhooks to plain
   HTTP, and the webhook is the *only* thing that can grant Pro.
3. **The volume must be confirmed durable.** If `/var/data` is ever lost, paid
   users lose their plan *and* their `stripe_customer_id` mapping — which means
   their renewals can never be attributed again. Take EBS snapshots before you
   take money.

Until `BILLING_CHECKOUT_URL_MONTHLY` / `_YEARLY` and `BILLING_WEBHOOK_SECRET` are
set, `billing_enabled` is `False`, no payment is possible, and the upgrade modal
shows a contact link. That is the correct state for staging.

See `backend/services/billing.py` for the design and `backend/.env.example` for
the Stripe setup steps.

## Known gaps

- **Backups.** Nothing snapshots the EBS volume yet. Add a DLM lifecycle policy
  before this holds anything you'd miss.
- **One instance, brief downtime.** `compose up -d` restarts containers in place;
  expect a few seconds of 502. SQLite wants a single writer, so scaling out means
  Postgres, not a second box.
- **Logs.** JSON to stdout, captured by the Docker journal on the instance.
  Nothing ships them to CloudWatch.
