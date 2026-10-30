[general]
static=yes
writeprotect=yes

[from-sip]
exten => _X.,1,NoOp(Incoming call to ${EXTEN} from ${CALLERID(num)})
 same => n,Answer()
 same => n,Wait(0.3)
 same => n,Set(CALLUUID=${UUID()})
 same => n,System(echo "${FILTER(0123456789+,${CALLERID(num)})}" > /calls/${CALLUUID})
 same => n,AudioSocket(${CALLUUID},127.0.0.1:${AUDIOSOCKET_PORT})
 same => n,Hangup()
exten => s,1,Goto(from-sip,000,1)

; Calls from Hermes: the plugin drops a call file into the outgoing spool. Ring for 20 s with the line's own
; number as caller ID; a call nobody answers is a free missed call. Not reachable from incoming calls.
[hermes-callout]
exten => _X.,1,GotoIf($[${EPOCH} > ${CALL_EXPIRES}]?expired) ; a request left behind while Asterisk was down
 same => n,ExecIf($["${CALLER_ID}" != ""]?Set(CALLERID(num)=${CALLER_ID})) ; set by the plugin per call
 same => n,Dial(PJSIP/${EXTEN}@sip,20)
 same => n(expired),Hangup()

; Answered: connect the call to the plugin under the id it chose
[hermes-answered]
exten => s,1,AudioSocket(${CALLUUID},127.0.0.1:${AUDIOSOCKET_PORT})
 same => n,Hangup()

[default]
exten => _X.,1,Hangup()
