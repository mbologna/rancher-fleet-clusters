# Credentials

This directory contains **Secret template files** for each infrastructure provider.
They are committed to the repo with placeholder values (`CHANGE_ME`) so you can:

1. Clone this repo to your private fork
2. Fill in the placeholder values in each file
3. Commit — Fleet will apply the Secrets before the providers start

> ⚠️ **Never commit real credentials to a public repository.**
> This repo is designed to be used as a **private fork**.

---

## AWS (CAPA)

File: `aws/capa-credentials-secret.yaml`

```yaml
stringData:
  credentials: |
    [default]
    aws_access_key_id = YOUR_KEY_ID
    aws_secret_access_key = YOUR_SECRET_KEY
```

Also run this **once per AWS account** to create the required IAM instance profiles:

```bash
clusterawsadm bootstrap iam create-cloudformation-stack
```

---

## Metal3 / Bare Metal (CAPM3)

File: `metal3/ironic-credentials-secret.yaml`

Fill in the Ironic API URL and HTTP basic auth credentials.
Copy the `bmc-secret-host-01` block once per physical host you want to provision,
renaming it to match the host (e.g. `bmc-secret-worker-01`, `bmc-secret-cp-01`).

The `BareMetalHost` CR in your cluster definition references the BMC secret by name:

```yaml
spec:
  bmc:
    address: "ipmi://192.168.1.10"
    credentialsName: bmc-secret-host-01
```
