# ChatGPT/Codex 26.901.20858 build 7658

The provider-picker UI patch no longer matches the frontend layout in build 7658, but the Desktop app still accepts `modelProvider` on `thread/start`.

`patch_chatgpt_provider_routing_26901.py` is a request-layer compatibility fallback for this build. It leaves the native model picker unchanged and applies the existing `~/.codex/desktop-model-providers.json` mapping when a new thread starts.

## What it does

- Reads `desktop-model-providers.json` for every new thread.
- Uses `model_providers[model_slug]` when an exact mapping exists.
- Uses `default_provider` for unmapped models.
- Patches both normal and prewarmed thread starts.
- Requests thread lists across providers when no provider filter was supplied.
- Keeps an explicitly supplied `modelProvider` unchanged.
- Preserves the original installer safety model: ASAR integrity verification, full app backup, fail-closed source matching, recovery on failed mutation, integrity metadata update, ad-hoc signing, and signature verification.

## Install

Keep the normal project configuration in place, including `~/.codex/desktop-model-providers.json`, then run:

```bash
python3 patch_chatgpt_provider_routing_26901.py
```

If `/Applications/ChatGPT.app` is not writable by the current account, rerun with `sudo` after reviewing the script:

```bash
sudo python3 patch_chatgpt_provider_routing_26901.py
```

The fallback does not add the old "Provider for new tasks" menu. Automatic routing is driven by the selected model and `desktop-model-providers.json`.

## Validation

The request-layer matcher was developed against the exact `app-initial-7a6c8787453d.js` bundle from ChatGPT/Codex 26.901.20858 build 7658.

Validation performed:

- Both expected insertion points matched exactly once.
- The patched 10 MB JavaScript bundle passed `node --check`.
- Mocked runtime routing verified exact model mapping, default-provider fallback, preservation of an explicit provider, and provider-unfiltered thread listing.
- A build-7658 prototype using the same two request-layer insertion points was tested successfully on macOS with `muse-spark-1.3-contributor` routed to a custom `meta_model_api` provider while built-in Codex models remained on the signed-in ChatGPT/OpenAI provider.
- The live test also exercised recovery: a first install attempt hit a filesystem permission error after backup creation and automatically restored the original app before a successful retry after permissions were corrected.

This remains an unofficial, build-specific compatibility patch. Future app updates may replace it or change the request-layer source; the installer rejects mismatched layouts instead of patching them blindly.
