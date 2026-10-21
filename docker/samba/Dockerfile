# A read-only SMB front-end for Frigate's media tree, so the recordings can be
# browsed from a Mac (Finder → ⌘K → smb://<vm-ip>) or any other client.
#
# Built from Debian's own samba package rather than a third-party "samba" image:
# the config is then a file you can read, and security fixes arrive through the
# Debian stream. The most popular image (`dperson/samba`) was last pushed in
# 2020; the maintained alternatives are configured entirely through environment
# variables, so you cannot see what smb.conf ends up as.
FROM debian:13-slim

# smbclient is not needed to serve shares — it is here so the share can be
# checked from inside the container (`smbclient //127.0.0.1/recordings ...`).
# Without it, "the Mac cannot see the share" has no server-side test at all.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      samba samba-common-bin smbclient netcat-openbsd \
 && rm -rf /var/lib/apt/lists/*

COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Documentation only — compose publishes this port.
EXPOSE 445

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
