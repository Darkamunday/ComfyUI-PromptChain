# PromptChain Global Prompt Library v1

This library uses PromptChain global scope:

```json
"scope": {
  "type": "global"
}
```

That means these packs are intended to appear across all supported models and architectures.

## Design rules used

- Model-neutral wording
- Descriptive prompts instead of model-specific tag spam
- Compatible with anime, semi-realistic, realistic, SDXL, Flux, Lumina, Qwen-style and other general image models
- No architecture-specific scope
- Readable IDs for debugging
- Categories kept simple for PromptChain UI filtering

## Suggested usage

Start with:
- Starter Presets
- Characters
- Hair
- Eyes
- Expressions
- Lighting
- Environments
- Quality
- Negative

Then combine with:
- Clothing
- Poses
- Camera
- Composition
- Mood
- Effects

## Notes

Some packs are broad, and some are practical utility packs for cleanup and workflow use.
The global library can later be split into Essentials, Complete, and Community packs.
