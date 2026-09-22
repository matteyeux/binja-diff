# Binary Diff

A Binary Ninja plugin for side-by-side binary diffing, powered by
[QBinDiff](https://github.com/quarkslab/qbindiff).

Drop a second binary onto the diff view and the plugin loads it, analyzes it,
matches its functions against the current binary, and shows the differences
as a control flow graph or a linear listing, in disassembly, LLIL, MLIL, HLIL,
Pseudo C, Pseudo Objective-C or Pseudo Rust.

![Capture](https://matteyeux.com/images/binja-diff2.png)

## Features

- No other tools required
- Supports BNDB import
- Graph and linear diff views, each in disassembly, LLIL, MLIL, HLIL, Pseudo C,
  Pseudo Objective-C or Pseudo Rust
- Save and restore diffs from the BNDB or json file
- Port function names from one binary to the other
- Diff a single kext from a kernelcache, or one SEP module
- QBinDiff as a provider for Binary Similarity sessions

## Requirements

- Python 3.10+
- [QBinDiff](https://github.com/quarkslab/qbindiff)

On Linux, QBinDiff also loads the system `libmagic` through `python-magic`, you may need to install `libmagic1` on Debian-based systems

### Linux aarch64 is not supported

`lapjv`, which QBinDiff uses for the linear assignment step, has **no working build on Linux aarch64** ([src-d/lapjv#90](https://github.com/src-d/lapjv/issues/90)): QBinDiff pulls in `lapjv-numpy2` there and that package is x86-only. macOS gets `lapx` instead and is unaffected. Testing has been done on **macOS and Linux x86-64**.

## Installation

I usually make symlinks:
```bash
# macOS
ln -s "$PWD" ~/Library/Application\ Support/Binary\ Ninja/plugins/binja-diff
# Linux
ln -s "$PWD" ~/.binaryninja/plugins/binja-diff
```

Restart Binary Ninja, then open a binary and pick **Diff** from the view type
dropdown at the bottom left, or use **Plugins > Binary Diff**.


## Usage

Drag a binary anywhere onto the drop zone, or click **Choose file...**. 

![Capture](https://matteyeux.com/images/binja-diff1.png)

Expect this to take a while on real firmware. Matching is quadratic in the
number of functions. sep-firmware M5 26.5 against 26.5.2 takes about 38 minutes.
Saving the result is worth it: restoring one costs only the reload of the second
binary.

### Binary Similarity

QBinDiff also registers as a provider for Binary Ninja's **Binary Similarity**
sidebar (Ultimate), alongside Google BinDiff and WARP, so a session can weigh
all three against each other. It works headlessly too:

```python
from binaryninja import SimilarityProviderType, SimilaritySession

provider_type = SimilarityProviderType["QBinDiff"]
session = SimilaritySession()
session.add_provider(provider_type.create(provider_type.get_default_settings()))
```

The similarity it reports is the share of a function's lines that did not
change, the same number the match table shows, because a resolver thresholds on
it to decide what to apply.

Rendering a result colours **individual instructions** rather than whole blocks:
yellow for a changed instruction, green for one only in the new build, red for
one only in the old. A block that exists on a single side is filled whole, and
lines that differ only in an address or a register are left plain — in a rebased
binary those are most of the lines, and colouring them hides the real change.

### Reading the diff

| Status | Meaning |
| --- | --- |
| identical | no differing instructions at all |
| offsets only | only addresses, immediates, stack offsets or register choice differ |
| changed | at least one real instruction difference |
| differs | too large to classify while scrolling; open it to see |


| Marker | Meaning |
| --- | --- |
| (blank) | identical |
| `~` | same operation, an operand spelled differently |
| `!` | genuinely different operation |
| `+` | present only in the secondary |
| `-` | present only in the primary |

### Dev

Ensure you run pre-commit before contributing
```bash
uv run pre-commit install
```
