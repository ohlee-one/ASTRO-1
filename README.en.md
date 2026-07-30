# Astro-1

[English](README.en.md) | [Français](README.md)

Automated astrophotography processing pipeline: from raw FITS files to a final
stretched, color-calibrated image ready to share.

Astro-1 is an **orchestrator**. It does not reimplement any image processing
algorithm — it drives existing open-source software via their command line,
based on simple YAML configuration profiles.

| Step | Engine | Role |
|---|---|---|
| Calibration, registration, stacking | [Siril](https://siril.org) | Mature, scriptable, GPL |
| Background extraction | [GraXpert](https://github.com/Steffenhir/GraXpert) | Pre-trained AI model |
| Stretch, color, sharpening, export | [Siril](https://siril.org) | Scripted CLI commands |

---

## Installation

### Prerequisites

1. **Siril** — [download](https://siril.org/download/) or `brew install --cask siril`
2. **GraXpert** — [download](https://github.com/Steffenhir/GraXpert/releases)
3. **Python 3.11+** and [uv](https://github.com/astral-sh/uv) (`brew install uv`)

Check that everything is in place:

```bash
uv run astro doctor
```

### Getting started

```bash
git clone https://github.com/ohlee-one/astro-1.git
cd astro-1
uv sync
```

### First GraXpert launch (important)

Launch GraXpert **in GUI mode once** and apply a treatment to any image. This
is the only way to download the AI models (CLI mode cannot fetch them). Once
the models are cached, CLI mode works offline.

---

## Organizing a session

The pipeline expects this folder structure for each session:

```
my-session/
├── lights/     ← your target frames (FITS)
├── darks/      ← your darks (or MasterDark_*.fit if pre-stacked)
├── flats/      ← your flats (or MasterFlat_*.fit if pre-stacked)
└── biases/     ← your offsets (optional)
```

The `process/`, `output/` and `logs/` folders are created automatically.

---

## Running a pipeline

```bash
uv run astro run \
  --session ~/Astro/2026-08-12_M42 \
  --setup redcat51-asi294mc \
  --target nebula-narrowband
```

The pipeline runs automatically:

1. **Siril** — calibration, alignment, stacking → linear FITS
2. **GraXpert** — background extraction (on the linear image)
3. **Siril** — Ha+OIII recomposition into RGB (narrowband mode only)
4. **GraXpert** — AI denoising (optional, on the linear RGB)
5. **Siril** — stretch, color, sharpening, export → final TIFF

The final image is in `output/final.tif`.

### Preview without executing

```bash
uv run astro run --session ... --setup ... --target ... --dry-run
```

Displays the generated Siril scripts and GraXpert commands **without running
anything**.

---

## Configuration profiles

Astro-1 works with two types of YAML profiles:

- **Setup** (`profiles/setups/`) — describes your equipment. Rarely changes.
  - Telescope, camera, Bayer pattern, calibration folders
- **Target** (`profiles/targets/`) — describes what you photograph. Changes every session.
  - Processing mode, stacking parameters, post-processing (stretch, color, etc.)

### Ready-to-copy templates

The `templates/` folder contains optimized starting profiles by object type
and equipment:

**Setups (equipment):**

| Template | Usage |
|---|---|
| `setup-color-dualband.yaml` | Color camera + dual-band filter (L-eXtreme, L-Ultimate) |
| `setup-color-lp.yaml` | Color camera + LP/UV-IR filter (broadband) |
| `setup-mono-narrowband.yaml` | Mono camera + narrowband filters (Ha, OIII, SII) |
| `setup-mono-lrgb.yaml` | Mono camera + LRGB filters |
| `setup-dslr.yaml` | DSLR / camera |

**Targets:**

| Template | Usage |
|---|---|
| `nebula-narrowband.yaml` | Narrowband nebula (Ha/OIII) |
| `nebula-rgb.yaml` | Broadband RGB nebula |
| `galaxy-rgb.yaml` | Galaxy |
| `cluster-rgb.yaml` | Star cluster (open or globular) |
| `comet-rgb.yaml` | Comet |
| `snr-narrowband.yaml` | Supernova remnant (Veil, Cygnus Loop…) |

### Interactive wizard

To get started even faster, use the interactive wizard:

```bash
uv run astro wizard
```

The wizard guides you step by step, **with no technical knowledge required**:

1. **Camera** — pick your model from a list (ASI294MC, ASI533MC, Canon 600D…) or enter a name. Pixel size is automatically inferred.
2. **Filter** — select your filter (L-eXtreme, L-Pro, Ha 7nm…). The processing type is automatically derived.
3. **Pre-stacked files** — indicate whether your darks/flats are already stacked by your acquisition software (ASIAIR, NINA) and whether you shoot separate bias frames.
4. **Target** — choose the object type (nebula, galaxy, cluster, comet…) and give it a name (M42, IC1805…).

YAML profiles are generated automatically in `profiles/`. No need to copy and
edit templates by hand.

---

## Iterating with an AI (Cursor, Claude, ChatGPT…)

The pipeline is designed to work with an AI assistant that adjusts profiles
for you. Two approaches:

### Simple approach (no MCP)

1. Open the project in [Cursor](https://cursor.sh) or your editor with an AI agent
2. Run the pipeline: `uv run astro run --session ... --setup ... --target ...`
3. If the result doesn't look right, describe what you want to the AI:
   - *"The image is too dark"* → the AI adjusts `stretch.shadows_clip` or `stretch.target_bg`
   - *"The nebula colors aren't vivid enough"* → the AI adjusts `color.saturation_boost`
   - *"The sky background is too bright"* → the AI adjusts `color.background_clip`
   - *"There's too much residual green"* → the AI changes `color.rmgreen_type` from `"average"` to `"maximum"`
4. Re-run the pipeline — iterate until you get the desired result

The AI reads the YAML profiles, understands the structure, and modifies the
right parameters. No knowledge of Siril or GraXpert needed.

### MCP approach (advanced — automated iteration)

Astro-1 includes an MCP (Model Context Protocol) server that exposes the
pipeline tools directly to your AI. The AI can run the pipeline, read logs,
and adjust profiles — all without you touching the terminal.

**Installation:**

```bash
uv sync --extra mcp
```

**Claude Desktop configuration:**

Add this block to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "astro-1": {
      "command": "uv",
      "args": ["run", "python", "-m", "astro_pipeline.mcp_server"],
      "cwd": "/path/to/astro-1"
    }
  }
}
```

**Cursor configuration:**

Create a `.mcp.json` file at the project root:

```json
{
  "mcpServers": {
    "astro-1": {
      "command": "uv",
      "args": ["run", "python", "-m", "astro_pipeline.mcp_server"]
    }
  }
}
```

**Exposed tools:**

| Tool | Role |
|---|---|
| `list_profiles_tool` | Lists available setup and target profiles |
| `doctor_tool` | Checks that Siril and GraXpert are installed |
| `run_pipeline_tool` | Runs the full pipeline on a session |
| `read_log_tool` | Reads the log of the last run |
| `get_profile_tool` | Retrieves the content of a YAML profile |
| `adjust_profile_tool` | Modifies a parameter in a YAML profile |

Once connected, you can tell Claude: *"Run the pipeline on my M42 session with
the redcat51 setup and the nebula-narrowband target, then if the image is too
dark, adjust the stretch."* — Claude does it all by itself.

### Key rendering parameters

| Parameter | File | Effect |
|---|---|---|
| `stretch.target_bg` | target | Background brightness (0.15 = dark, 0.35 = bright) |
| `stretch.shadows_clip` | target | Shadow retention (-2.8 = standard, -1.0 = more contrast) |
| `color.saturation_boost` | target | Global saturation (0.5 = +50%, 1.0 = +100%) |
| `color.saturation_threshold` | target | Noise threshold (0 = saturate everything, 1.5 = only bright areas) |
| `color.target_hue_boost` | target | Targeted hue saturation (5 = magenta-pink) |
| `color.background_clip` | target | Darken background (0.02 = subtle, 0.06 = pronounced) |
| `color.rmgreen_type` | target | Green removal ("average" = gentle, "maximum" = aggressive) |
| `sharpening.amount` | target | Sharpening strength (0.5 = moderate, 1.0 = strong) |

---

## Available commands

```bash
uv run astro doctor          # Checks that Siril and GraXpert are installed
uv run astro run ...          # Runs the full pipeline
uv run astro run ... --dry-run  # Displays scripts without executing
```

---

## Architecture

```
src/astro_pipeline/
├── cli.py          → entry point (Typer CLI)
├── pipeline.py     → orchestrates phases, handles paths and logging
├── config.py       → Pydantic models + YAML profile loading/merging
├── log.py          → persistent per-session logger (file + Rich console)
└── engines/
    ├── siril.py    → generates .ssf scripts and runs siril-cli
    └── graxpert.py → builds and runs GraXpert commands
```

Dependency rule: `cli` → `pipeline` → `engines` → `config`. Never the other way.

---

## Known limitations

- **GraXpert denoising on Apple Silicon**: may crash with an `onnxruntime`/`CoreML`
  bug (`KernelChannels != InputChannels`). Disabled by default in templates.
- **StarNet++**: star/starless separation works, but recombination
  (processed starless + star mask) is not yet implemented. Disabled by default.
- **macOS only** for now. The pipeline should work on Linux with adapted
  install paths, but this is untested.

---

## License

MIT — see [LICENSE](LICENSE).

Astro-1 uses and drives open-source software (Siril: GPL, GraXpert: MIT) which
retain their own licenses.