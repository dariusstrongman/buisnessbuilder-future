# Exact Worker 4 integration archive

This branch durably preserves the original local-only integration history as a base64-encoded Git bundle.

- Original branch: `integration/offline-billy-bob-v1`
- Original commit: `2c8549c6579409d6b114bfb2a8436d4ec23ecfca`
- Original tree: `11b57ddc0e768cfbc7e1cdb7a7cd750383754c80`
- Required base: `b5a44761ce83f0032684f825829123380573536f`
- Published equivalent-tree commit: `fbaee89ae3cd504602f69e5a04d4720248fc515f`

Restore and verify:

```bash
base64 --decode archive/offline-billy-bob-v1-original.bundle.b64 > offline-billy-bob-v1-original.bundle
git bundle verify offline-billy-bob-v1-original.bundle
git fetch offline-billy-bob-v1-original.bundle integration/offline-billy-bob-v1:refs/heads/archive/restored-offline-billy-bob-v1
git rev-parse archive/restored-offline-billy-bob-v1
```

The final command must print `2c8549c6579409d6b114bfb2a8436d4ec23ecfca`.
