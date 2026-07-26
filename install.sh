#!/bin/bash

set -e

envs=(benchmark-euler-kernels ddc gmgpolar gyselalibxx heraclespp kokkos kokkos-fft)

for env in "${envs[@]}"
do
  spack env remove --yes-to-all ${env} || true
  spack env create ${env} dev-${env}.yaml
  spack --env ${env} install --use-buildcache only
done
