import sys
import os

try:
    import vllm
except ImportError:
    print("vllm not installed")
    sys.exit(1)

vllm_dir = os.path.dirname(vllm.__file__)

# ── Patch 1: deep_gemm_warmup.py ──────────────────────────────────────────────
# In vLLM 0.20+, `get_mk_alignment_for_contiguous_layout()` is called at the
# TOP of `_fp8_linear_may_use_deep_gemm` BEFORE any guard, so if deep_gemm is
# not installed it raises a RuntimeError and crashes the engine.
# Fix: wrap that first call in a try/except and return False early.

warmup_file = os.path.join(
    vllm_dir, "model_executor", "warmup", "deep_gemm_warmup.py"
)

if not os.path.exists(warmup_file):
    print(f"File not found: {warmup_file}, skipping Patch 1.")
else:
    with open(warmup_file, "r") as f:
        content = f.read()

    OLD = '    block_size = get_mk_alignment_for_contiguous_layout()[0]\n'
    NEW = (
        '    try:\n'
        '        block_size = get_mk_alignment_for_contiguous_layout()[0]\n'
        '    except RuntimeError:\n'
        '        return False\n'
    )

    # Only patch inside _fp8_linear_may_use_deep_gemm
    func_start = content.find('def _fp8_linear_may_use_deep_gemm')
    # find the first occurrence of OLD after func_start
    pos = content.find(OLD, func_start)
    if pos == -1 or content[func_start:pos].count('\ndef ') > 0:
        print("Patch 1: pattern not found inside _fp8_linear_may_use_deep_gemm, skipping.")
    elif NEW in content:
        print("Patch 1: already applied.")
    else:
        new_content = content[:pos] + NEW + content[pos + len(OLD):]
        with open(warmup_file, "w") as f:
            f.write(new_content)
        print("Patch 1: Successfully patched deep_gemm_warmup.py")

# ── Patch 2: deep_gemm_warmup.py – _count_warmup_iterations ──────────────────
# Same function is also called in _count_warmup_iterations at line ~342.
# Wrap every other bare call in a try/except as well.

if os.path.exists(warmup_file):
    with open(warmup_file, "r") as f:
        content = f.read()

    COUNT_OLD = '        if _fp8_linear_may_use_deep_gemm(m):\n'
    # _fp8_linear_may_use_deep_gemm already returns False safely after Patch 1,
    # so _count_warmup_iterations is fine.  Nothing more needed here.

    # Also guard the standalone get_mk_alignment calls in other functions
    STANDALONE = 'block_m = get_mk_alignment_for_contiguous_layout()[0]\n'
    if STANDALONE in content:
        wrapped = (
            'try:\n'
            '            block_m = get_mk_alignment_for_contiguous_layout()[0]\n'
            '        except RuntimeError:\n'
            '            return\n'
        )
        # Don't double-patch
        if wrapped not in content:
            content = content.replace(
                '        ' + STANDALONE,
                '        ' + wrapped
            )
            with open(warmup_file, "w") as f:
                f.write(content)
            print("Patch 2: Guarded standalone block_m calls.")
        else:
            print("Patch 2: already applied.")
    else:
        print("Patch 2: no standalone block_m calls found (ok).")
