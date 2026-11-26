#!/bin/bash

set -e

spack bootstrap now

envs=(benchmark-euler-kernels ddc gmgpolar gyselalibxx gyselaxpp heraclespp kokkos kokkos-comm kokkos-fft mini-app-io mini-app-poisson)

root="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"

for env in "${envs[@]}"
do
  spack env remove --yes-to-all ${env} || true
  spack env create ${env} ${root}/dev-${env}.yaml
  spack --env ${env} repo update
  spack --env ${env} buildcache list
  spack --env ${env} install --include-build-deps --use-buildcache only
done
