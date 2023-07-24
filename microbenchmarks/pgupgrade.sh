#!/bin/zsh

OLD_PGDATA=/mnt/nvme/postgresql/REL_16_STABLE/data_mdam_table
# OLD_PGDATA=/mnt/nvme/postgresql/REL_16_STABLE/data_imdb
NEW_PGDATA=/mnt/nvme/postgresql/patch/data
OLD_BINDIR=/mnt/nvme/postgresql/REL_16_STABLE/install_meson_rc/bin
NEW_BINDIR=/mnt/nvme/postgresql/patch/install_meson_rc/bin

rm -rf $NEW_PGDATA
/mnt/nvme/postgresql/patch/install_meson_rc/bin/initdb \
  --encoding=UTF8 \
  --locale-provider=icu \
  --locale=en_US.UTF8 \
  --icu-locale=en \
  -D $NEW_PGDATA

/mnt/nvme/postgresql/patch/install_meson_rc/bin/pg_upgrade \
  --old-datadir $OLD_PGDATA \
  --new-datadir $NEW_PGDATA \
  --old-bindir $OLD_BINDIR \
  --new-bindir $NEW_BINDIR

echo "jit=off" >> $NEW_PGDATA/postgresql.conf
echo "include '$HOME/dotfiles/postgresql.conf'" >> $NEW_PGDATA/postgresql.conf
rm delete_old_cluster.sh
