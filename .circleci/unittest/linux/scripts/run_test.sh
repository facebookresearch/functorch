#!/usr/bin/env bash

set -e

export IN_CI=1
mkdir test-reports
eval "$(./conda/bin/conda shell.bash hook)"
conda activate ./env

python -m torch.utils.collect_env

# test_functorch_lagging_op_db.py: Only run this locally because it checks
# the functorch lagging op db vs PyTorch's op db.
EXIT_STATUS=0

git_version=$(python -c "import torch.version; print(torch.version.git_version)")
echo git_version

git clone https://github.com/pytorch/pytorch.git
pushd pytorch
git checkout $git_version
pushd test
pushd functorch

find . \( -name test\*.py \) | xargs -I {} -n 1 python {} -v || EXIT_STATUS=$?
exit $EXIT_STATUS
