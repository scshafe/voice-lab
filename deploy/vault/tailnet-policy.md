# Vault tailnet policy

Apply only with Cole during deployment. These HuJSON fragments extend the live
policy; they are not a replacement policy. Replace both placeholders with Cole's
actual login and the recording Mac's stable tailnet IPv4 address.

```jsonc
"tagOwners": {
  "tag:voice-lab-vault": ["<cole's tailnet login>"],
},
"hosts": {
  "cole-voice-recording-mac": "<recording Mac tailnet IPv4 address>",
},
"grants": [
  {
    "src": ["cole-voice-recording-mac"],
    "dst": ["tag:voice-lab-vault"],
    "ip": ["tcp:443"],
  },
],
```

No grant has the vault tag as its source. It receives no SSH grant, subnet route,
exit-node role, or Funnel node attribute. Tailnet Lock stays off (Decision 3).
The vault sidecar's egress network is for Tailscale coordination and certificates;
the vault application has only the internal Docker network.

Tailscale grants and legacy ACLs are additive. Inspect **all** existing grants,
ACLs, groups, autogroups, wildcards, and node attributes before saving: an older
allow-all rule can still allow unrelated peers into the vault or let the vault
initiate connections. Narrow such a rule with Cole rather than assuming this new
grant overrides it. Check the admin console's policy preview/tests for:

- Recording Mac → vault TCP 443: allowed.
- Another ordinary tailnet peer → vault TCP 443: denied.
- Recording Mac → vault TCP 8080 and SSH: denied.
- Vault → an ordinary peer's application and SSH ports: denied.

Confirm those cases against the live policy before the first upload. Changing
the Mac's tailnet address requires updating the host alias. Tagged node ownership
is bound to this dedicated tag, not to a reusable shared `tag:container` key.

## The Air's vault (phase 3)

The Air's vault is its own node, `voice-lab-vault-air`, with its own tag, so the PC's grant
to it is separate from the laptop's and either can be removed alone. In infra's
`tailnet/policy.hujson` (the live policy since phase 3 of the toolshed split), with the
PC's node as the source the laptop vault's grant already uses:

```jsonc
"tagOwners": {
  "tag:voice-lab-vault-air": ["<cole's tailnet login>"],
},
"grants": [
  {
    "src": ["<the PC's node or tag, as in the laptop vault's grant>"],
    "dst": ["tag:voice-lab-vault-air"],
    "ip": ["tcp:443"],
  },
],
```

The same rules and checks as above hold, for the Air's tag: no grant has it as its source,
no SSH, no subnet route, exit node or Funnel; the PC reaches TCP 443 and nothing else; an
ordinary peer is denied. The Air's own node and its user's grants are unchanged. The
approval for the Air names `tag:voice-lab-vault-air` (`vault approve --tag`), and the PC's
copier checks it with the node's StableID and DNS name before every session.
