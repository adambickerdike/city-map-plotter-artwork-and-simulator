# Git LFS usage

All `*.png`, `*.svg` and `*.plot.json` files are tracked by Git LFS. The
122 MiB copied F1 source catalog is also tracked explicitly. The files remain
present at their ordinary repository paths; Git stores small pointer records
while GitHub stores the corresponding content objects.

## First clone

```bash
git lfs install
git clone https://github.com/adambickerdike/city-map-plotter-artwork-and-simulator.git
cd city-map-plotter-artwork-and-simulator
git lfs pull
git lfs ls-files
python3 scripts/verify_repository.py --full
```

If a file begins with `version https://git-lfs.github.com/spec/v1`, it is still
an LFS pointer. Run `git lfs pull`.

## Why LFS is required here

The complete repository contains 447 PNGs, 455 SVGs and 424 plot manifests.
The image/vector files exceed 2 GiB before Git compression, while two JSON
objects exceed GitHub's enforced 100 MiB regular-Git limit. LFS avoids an
oversized Git pack and keeps the ordinary Git history focused on code and
documentation.

Official references:

- https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage
- https://docs.github.com/en/repositories/creating-and-managing-repositories/repository-limits
- https://docs.github.com/en/billing/concepts/product-billing/git-lfs

GitHub source ZIP/TAR archives contain LFS pointers unless the repository owner
enables LFS objects in archives. A Git clone followed by `git lfs pull` is the
canonical retrieval method for this release.
