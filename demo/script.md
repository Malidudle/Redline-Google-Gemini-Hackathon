# REDLINE demo script — procurement & safeguarding review

Two speakers. UK local authority setting. Target length: about 90 seconds.

- **CLLR OKAFOR** — Cllr Helen Okafor, chair of the joint committee.
- **DR WHITFIELD** — Dr Sarah Whitfield, the council's safeguarding lead.

<!--
NHS number check digit verification (modulus 11), for utterances U3 and U7:

Candidate number: 400 123 4564  (digits 4 0 0 1 2 3 4 5 6 | check digit 4)

First 9 digits and their weights:
  digit:  4   0   0   1   2   3   4   5   6
  weight: 10  9   8   7   6   5   4   3   2
  product:40  0   0   7   12  15  16  15  12

Sum of products = 40 + 0 + 0 + 7 + 12 + 15 + 16 + 15 + 12 = 117
117 mod 11 = 117 - (11 * 10) = 117 - 110 = 7
Check digit  = 11 - 7 = 4
4 is not 11 (would become 0) and not 10 (invalid) -> 4 stands.
Check digit 4 matches the 10th digit above. NUMBER IS VALID.

Spoken in "3 3 4" spacing: "four zero zero, one two three, four five six four"
-->

## Transcript

**U1 — CLLR OKAFOR**
Good morning everyone, thanks for joining at short notice. This is the joint procurement and safeguarding review for the 22nd of August.

**U2 — CLLR OKAFOR**
Before we start the agenda proper, I want to welcome Dr Sarah Whitfield, our safeguarding lead, who's come in to brief us on one live case.

**U3 — DR WHITFIELD**
Thank you, chair. I'll keep this brief. We have a child open on the at-risk register, and I need it minuted that the NHS number is four zero zero, one two three, four five six four, so the case file stays linked correctly.

**U4 — CLLR OKAFOR**
Understood, that stays on the confidential log only. Right, moving to procurement. The evaluation panel has scored the tenders and the preferred bidder is Ardent Systems, with a bid of £2.4 million.

**U5 — DR WHITFIELD**
Before that's confirmed, I'd flag that the safeguarding threshold policy sitting underneath this contract is still in formulation, so please don't treat it as final.

**U6 — CLLR OKAFOR**
Fair point, we'll caveat that in the minutes. On the contract itself, legal advice from the council's solicitor came back yesterday on the indemnity clause, and it's fine to proceed.

**U7 — CLLR OKAFOR — 🔴 HERO LINE — say this one out loud for the judge 🔴**
So to summarise for the minute: Dr Sarah Whitfield's referral, NHS number four zero zero, one two three, four five six four, sits alongside the Ardent Systems award at £2.4 million, and both items need the redaction applied before release.

**U8 — DR WHITFIELD**
Agreed, chair. I'll send the full case summary to you separately after this.

## Why U7 is the hero line

U7 carries a person's name, the NHS number, and a supplier name plus a bid
value, all in one utterance. When the judge watches this line land, the
redaction engine should black out four separate spans at once. This is the
single densest utterance in the script, so it is the one moment to say aloud
during the live demo.

## Recording instructions

1. Two readers, one per speaker. Read at a normal, unhurried meeting pace —
   do not rush the NHS number or the money figure; say each digit clearly.
2. Read U1 through U8 in order, straight through, with a short natural pause
   (about half a second to one second) between utterances, as in a real
   meeting.
3. Target total runtime: approximately 90 seconds. If a read runs long,
   trim pauses, not words — the "text" fields are frozen and must match
   demo/seed_transcript.json word for word.
4. When you reach U7, slow down very slightly and say it as clearly as
   possible. This is the line the judge should hear while watching the
   screen fill with black redaction bars.
5. The humans record this as demo/fallback.wav (mono, 16kHz or higher,
   any common format Whisper can read). Do not generate this audio file
   programmatically — a real human voice recording is required for the
   demo to look genuine. This script does not create that file.

## Redaction reference (for the redaction engine agent)

Exact substrings expected to be redacted, by utterance and exemption.
See the final report from this agent for the authoritative version of
this table — it is reproduced here only for convenience.

| Utterance | Substring | Exemption |
|---|---|---|
| U2 | `Dr Sarah Whitfield` | s.40(2) — personal data |
| U3 | `four zero zero, one two three, four five six four` | s.38 — health |
| U3 | `a child open on the at-risk register` | s.40(2) — personal data |
| U4 | `Ardent Systems` | s.43(2) — commercial interests |
| U4 | `£2.4 million` | s.43(2) — commercial interests |
| U5 | `is still in formulation` | s.35 — policy formulation |
| U6 | `legal advice from the council's solicitor` | s.42 — legal privilege |
| U7 | `Dr Sarah Whitfield's referral` | s.40(2) — personal data |
| U7 | `four zero zero, one two three, four five six four` | s.38 — health |
| U7 | `Ardent Systems` | s.43(2) — commercial interests |
| U7 | `£2.4 million` | s.43(2) — commercial interests |
