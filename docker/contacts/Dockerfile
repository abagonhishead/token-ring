# Contacts: a CardDAV server (Radicale, https://radicale.org, GPL-3.0) for the phones' address
# book. The web page keeps it, as the user "token-ring": your contacts (the Contacts tab) and the
# PBX directory (the extensions and session lines, kept up to date by the page), and copies it
# into the home of each phone's read-only login (where CardDAV apps look). The passwords are in
# the contacts_users secret, one "user:password" line each: token-ring's, then one per phone
# (compose.yaml says how to make it).
# HTTPS with /tls/fullchain.pem and privkey.pem when they're there, on port 5232.
FROM python:3.13-slim-trixie
RUN pip install --no-cache-dir radicale==3.5.5
COPY docker/contacts/config docker/contacts/rights /etc/radicale/
RUN groupadd --system --gid 1000 radicale && useradd --system --uid 1000 --gid 1000 --no-create-home radicale \
    && mkdir -p /var/lib/radicale/collections && chown -R radicale:radicale /var/lib/radicale
USER radicale
VOLUME /var/lib/radicale
CMD ["sh", "-c", "if [ -r /tls/fullchain.pem ] && [ -r /tls/privkey.pem ]; then exec python -m radicale --server-ssl True --server-certificate /tls/fullchain.pem --server-key /tls/privkey.pem; else exec python -m radicale; fi"]
