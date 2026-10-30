[global]
type=global
user_agent=HermesPhone
endpoint_identifier_order=ip

[udp]
type=transport
protocol=udp
bind=0.0.0.0:${SIP_PORT}

[sip-reg]
type=registration
transport=udp
outbound_auth=sip-auth
server_uri=sip:${SIP_SERVER}
client_uri=sip:${SIP_USER}@${SIP_SERVER}
contact_user=${SIP_USER}
retry_interval=60
forbidden_retry_interval=600
expiration=300

[sip-auth]
type=auth
auth_type=userpass
username=${SIP_USER}
password=${SIP_PASSWORD}

[sip-aor]
type=aor
contact=sip:${SIP_SERVER}
qualify_frequency=30

[sip]
type=endpoint
identify_by=ip
transport=udp
context=from-sip
disallow=all
allow=alaw,ulaw
outbound_auth=sip-auth
aors=sip-aor
from_user=${SIP_USER}
from_domain=${SIP_SERVER}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
dtmf_mode=rfc4733

[sip-identify]
type=identify
endpoint=sip
match=${SIP_SERVER}
${SIP_IDENTIFY_EXTRA}
