#!/usr/bin/env bash
#
# One-time AWS setup: the ECR repositories and IAM wiring that the deploy
# pipeline needs. Run once, from your laptop:
#
#   aws configure --profile testra      # if you haven't already
#   bash deploy/aws-setup.sh testra
#
# It creates: 2 ECR repos (+ lifecycle policies), the GitHub OIDC provider, the
# role GitHub Actions assumes, and the role the EC2 instance runs as.
#
# It does NOT create the EC2 instance, volume, or Elastic IP — those are worth
# doing in the console where you can see the instance type and what it costs
# before you press the button. See docs/DEPLOY.md step 3.
#
# Safe to re-run: everything here checks before it creates.
set -euo pipefail

PROFILE="${1:-}"
[[ -n "$PROFILE" ]] || { echo "usage: bash deploy/aws-setup.sh <aws-profile>" >&2; exit 1; }

# ── The guard that matters ────────────────────────────────────────────────
# There are two AWS accounts in play on this machine: this project's, and an
# employer's production account (391505363072) whose profile sits right next to
# this one. Creating this stack in the wrong one would be tedious to unpick and
# embarrassing to explain, so the account is asserted, not assumed.
EXPECTED_ACCOUNT="920226265373"
REGION="${AWS_REGION:-ap-south-1}"
# Must be the repo's CURRENT name. GitHub redirects old names for git, so a
# stale value here pushes and clones fine — and then fails only at deploy time,
# because the OIDC token's `sub` always carries the current name and won't match
# a trust policy pinned to the old one. (This repo was renamed from testgen-ai.)
GITHUB_REPO="${GITHUB_REPO:-Krishna417211/ai-test-generator}"
GITHUB_ENVIRONMENT="${GITHUB_ENVIRONMENT:-staging}"

DEPLOY_ROLE="testra-github-deploy"
INSTANCE_ROLE="testra-instance"
REPOS=(testra-backend testra-web)

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }
log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

ACTUAL_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)" \
    || die "profile '$PROFILE' cannot authenticate"

if [[ "$ACTUAL_ACCOUNT" != "$EXPECTED_ACCOUNT" ]]; then
    die "profile '$PROFILE' is account $ACTUAL_ACCOUNT, expected $EXPECTED_ACCOUNT.
Refusing to create resources in the wrong account. If $ACTUAL_ACCOUNT really is
the right one now, change EXPECTED_ACCOUNT at the top of this script."
fi

CALLER="$(aws sts get-caller-identity --query Arn --output text)"
cat <<EOF

  Account : $ACTUAL_ACCOUNT
  Identity: $CALLER
  Region  : $REGION
  Repo    : $GITHUB_REPO (environment: $GITHUB_ENVIRONMENT)

This creates ECR repositories and IAM roles. ECR storage past the free tier
costs money; IAM roles are free.
EOF
read -rp $'\nProceed? [y/N] ' answer
[[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Aborted."; exit 0; }

# ── ECR ───────────────────────────────────────────────────────────────────
for repo in "${REPOS[@]}"; do
    if aws ecr describe-repositories --repository-names "$repo" >/dev/null 2>&1; then
        log "ECR repo $repo already exists"
    else
        log "Creating ECR repo $repo"
        aws ecr create-repository \
            --repository-name "$repo" \
            --image-scanning-configuration scanOnPush=true \
            >/dev/null
    fi

    # CI tags every image with its commit SHA and never deletes. Without this the
    # registry grows forever; ECR's free tier is only 500MB. Ten is a floor, not
    # a target: rollback means redeploying an older SHA, and an expired image
    # cannot be rolled back to.
    log "Setting lifecycle policy on $repo (keep last 10)"
    aws ecr put-lifecycle-policy --repository-name "$repo" --lifecycle-policy-text '{
      "rules": [{
        "rulePriority": 1,
        "description": "Keep the 10 most recent images; expire the rest",
        "selection": { "tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 10 },
        "action": { "type": "expire" }
      }]
    }' >/dev/null
done

# ── GitHub OIDC provider ──────────────────────────────────────────────────
OIDC_ARN="arn:aws:iam::${ACTUAL_ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$OIDC_ARN" >/dev/null 2>&1; then
    log "GitHub OIDC provider already registered"
else
    log "Registering GitHub as an OIDC identity provider"
    # This is what lets GitHub Actions get short-lived AWS credentials, so no
    # long-lived access key ever has to live in the repo.
    aws iam create-open-id-connect-provider \
        --url https://token.actions.githubusercontent.com \
        --client-id-list sts.amazonaws.com \
        >/dev/null
fi

# ── Role that GitHub Actions assumes ──────────────────────────────────────
# The sub condition is the whole security boundary: it pins assumption to this
# one repo AND this one environment. Because the deploy job declares
# `environment: staging`, the token's sub is "...:environment:staging" — NOT
# "...:ref:refs/heads/master". Using the ref form here is the classic cause of
# an opaque AccessDenied. Never widen this to a wildcard.
TRUST=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "$OIDC_ARN" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:${GITHUB_REPO}:environment:${GITHUB_ENVIRONMENT}"
      }
    }
  }]
}
EOF
)

if aws iam get-role --role-name "$DEPLOY_ROLE" >/dev/null 2>&1; then
    log "Role $DEPLOY_ROLE exists — updating its trust policy"
    aws iam update-assume-role-policy --role-name "$DEPLOY_ROLE" --policy-document "$TRUST"
else
    log "Creating role $DEPLOY_ROLE"
    aws iam create-role \
        --role-name "$DEPLOY_ROLE" \
        --description "GitHub Actions deploys Testra: push to ECR, roll the instance via SSM" \
        --assume-role-policy-document "$TRUST" \
        >/dev/null
fi

# Scoped to these two repos and the SSM run-command document. The instance is
# left as * because it doesn't exist yet; tighten it to the instance ARN once
# you've launched it (see docs/DEPLOY.md).
log "Attaching the deploy permissions"
aws iam put-role-policy --role-name "$DEPLOY_ROLE" --policy-name testra-deploy --policy-document "$(cat <<EOF
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
        "arn:aws:ecr:${REGION}:${ACTUAL_ACCOUNT}:repository/testra-backend",
        "arn:aws:ecr:${REGION}:${ACTUAL_ACCOUNT}:repository/testra-web"
      ]
    },
    { "Effect": "Allow", "Action": "ssm:SendCommand",
      "Resource": [
        "arn:aws:ec2:${REGION}:${ACTUAL_ACCOUNT}:instance/*",
        "arn:aws:ssm:${REGION}::document/AWS-RunShellScript"
      ]
    },
    { "Effect": "Allow",
      "Action": ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
      "Resource": "*"
    }
  ]
}
EOF
)"

# ── Role the instance runs as ─────────────────────────────────────────────
if aws iam get-role --role-name "$INSTANCE_ROLE" >/dev/null 2>&1; then
    log "Role $INSTANCE_ROLE already exists"
else
    log "Creating role $INSTANCE_ROLE"
    aws iam create-role \
        --role-name "$INSTANCE_ROLE" \
        --description "Testra EC2: managed by SSM, pulls images from ECR" \
        --assume-role-policy-document '{
          "Version": "2012-10-17",
          "Statement": [{
            "Effect": "Allow",
            "Principal": { "Service": "ec2.amazonaws.com" },
            "Action": "sts:AssumeRole"
          }]
        }' >/dev/null
fi

# SSM: how CI reaches the box without SSH or an open port 22.
# ECR read-only: how the box pulls the images CI pushed. Read-only on purpose —
# a compromised instance must not be able to overwrite your images.
for policy in \
    arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore \
    arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
do
    aws iam attach-role-policy --role-name "$INSTANCE_ROLE" --policy-arn "$policy"
done

if aws iam get-instance-profile --instance-profile-name "$INSTANCE_ROLE" >/dev/null 2>&1; then
    log "Instance profile $INSTANCE_ROLE already exists"
else
    log "Creating instance profile $INSTANCE_ROLE"
    aws iam create-instance-profile --instance-profile-name "$INSTANCE_ROLE" >/dev/null
    aws iam add-role-to-instance-profile \
        --instance-profile-name "$INSTANCE_ROLE" --role-name "$INSTANCE_ROLE"
fi

# ── Done ──────────────────────────────────────────────────────────────────
DEPLOY_ROLE_ARN="$(aws iam get-role --role-name "$DEPLOY_ROLE" --query Role.Arn --output text)"
REGISTRY="${ACTUAL_ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

cat <<EOF

$(printf '\033[1m==> Done.\033[0m')

  ECR registry     : $REGISTRY
  Deploy role ARN  : $DEPLOY_ROLE_ARN
  Instance profile : $INSTANCE_ROLE

Next (docs/DEPLOY.md step 3):

  1. Launch the EC2 instance in $REGION — Amazon Linux 2023, t3.micro if you
     want free tier, instance profile "$INSTANCE_ROLE", security group opening
     only 80 and 443. Attach a separate 20GB gp3 volume and an Elastic IP.
  2. On the box: sudo bash /tmp/testra/deploy/bootstrap.sh /dev/nvme1n1
  3. Fill in /var/data/testra.env
  4. In GitHub → Settings → Environments → "$GITHUB_ENVIRONMENT":
       secret   AWS_DEPLOY_ROLE_ARN = $DEPLOY_ROLE_ARN
       variable AWS_REGION          = $REGION
       variable EC2_INSTANCE_ID     = i-...
       variable SITE_URL            = https://your-domain
       variable SITE_ADDRESS        = your-domain
  5. Merge to master.

EOF
