#!/bin/zsh

PGDATA=/mnt/nvme/postgresql/patch/data

rm -rf $PGDATA
/mnt/nvme/postgresql/patch/install_meson_rc/bin/initdb \
  --encoding=UTF8 \
  --locale-provider=icu \
  --locale=en_US.UTF8 \
  --icu-locale=en \
  -D $PGDATA

/mnt/nvme/postgresql/patch/install_meson_rc/bin/pg_upgrade \
  --old-datadir /mnt/nvme/postgresql/REL_16_STABLE/data_mdam_table \
  --new-datadir /mnt/nvme/postgresql/patch/data \
  --old-bindir /mnt/nvme/postgresql/REL_16_STABLE/install_meson_rc/bin \
  --new-bindir /mnt/nvme/postgresql/patch/install_meson_rc/bin

echo "jit=off" >> $PGDATA/postgresql.conf
echo "include '$HOME/dotfiles/postgresql.conf'" >> $PGDATA/postgresql.conf
