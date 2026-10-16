# OpenTofu state for the GitHub Actions beta deploy (.github/workflows/deploy-beta.yml), on
# Cloudflare R2 through the S3 backend. infra/ci/beta.sh `tofu-init` copies this file into
# infra/terraform/ on the runner and nowhere else, so a local `tofu init` there keeps its local state
# exactly as before.
#
# The bucket (`beta-tfstate`, created by hand once, docs/64 §2) and the account's endpoint
# (https://<account id>.r2.cloudflarestorage.com) come from the -backend-config file beta.sh writes;
# the credentials are the R2 S3 token, as AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. The flags
# below are what R2 needs from the S3 backend: no AWS region, account or instance-metadata lookups,
# path-style addressing, and no S3 checksum headers.
#
# No state lock: R2 is not DynamoDB, and the workflow's `concurrency` group runs one deploy at a
# time. Nobody else applies this state.
terraform {
  backend "s3" {
    key                         = "single-host/production.tfstate"
    region                      = "auto"
    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}
