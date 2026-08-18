# University memorabilia v2.1 renderer contract

This directory makes the York-derived university-map renderer reproducible
without relying on a generated `review-output/` directory.

- `base/renderer-contract.tar` is the immutable audited v2.1 archive.
- `base/renderer-contract/` is the byte-equivalent extracted source tree.
- `overrides/` contains the reviewed visual-correctness overrides plus the
  v2.1.4 batch/CLI source-pinning backport used by
  `tools/build_ranked_university_series.py`.
- `source-snapshots/` contains the 50 subject-keyed, hash-bound OpenStreetMap
  responses used by the reviewed 30 UK / 20 US cohort. The builder uses these
  saved inputs by default and cannot silently replace one with a live response.
- `render-recipe-v2.1.4.json` is the compact machine-readable index of the
  renderer, catalog, style, format, font, source, environment, and every
  rendering parameter that defines this edition.

The builder verifies every relevant digest and fails closed if these inputs
change. The principal pins are:

| Input | SHA-256 |
| --- | --- |
| Base archive | `794e4a44716e3739d22200370203a171cad05a52f79b5f00949c461fa46998f7` |
| Base renderer tree | `648f15740f25916df854045581adbe20df067bbb55d30ecf8fe3a366d4db286d` |
| Base style | `d5bc3c092d6cc05bbbc9581b5463a043716cfd5a8b237f8df634a44d6b6f7910` |
| Derived renderer tree | `2197dace775a09bd73d70cc9233be3689f5439f0e10942000ebc191f42065e4c` |
| Derived renderer fingerprint | `0a2106ff042bcccb7e73ad5fe3d253d0a5c7c18f2ca2425a35ea33aa29f69366` |
| Render recipe | `a3a8e6932fe4d6175e90ec25d5ce30922dad39c434297ef9a6a722a96dd153fc` |
| Source manifest file | `581d07bc7262664d4b1134ec42b30ddd9a086f9915e067068b4f0fe77e121362` |
| Source cohort | `1a861b085466c23ce2c97ce03b3807459bf96cf3e0c3c0faccbf3079b97cd6d3` |

This is a rendering and review contract, not a claim that nominal pen widths
are calibrated for a particular physical pen, paper, speed, or pressure. Run
the physical calibration and preview gates before plotting production work.

For the clean-clone environment, build, and verification commands, use
[`docs/reproducibility/REPRODUCING_MAPS.md`](../../docs/reproducibility/REPRODUCING_MAPS.md).
