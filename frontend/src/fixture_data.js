// Copy of demo/seed_transcript.json plus the redaction spans from the demo script.
// Lives here so fixture mode needs no backend and no files outside frontend/.

export const FIXTURE_SEGMENTS = [
  {
    "id": "174a632e",
    "speaker": "CLLR OKAFOR",
    "text": "Good morning everyone, thanks for joining at short notice. This is the joint procurement and safeguarding review for the 22nd of August.",
    "t_start": 0.0,
    "t_end": 8.0,
    "spans": []
  },
  {
    "id": "93f3bc86",
    "speaker": "CLLR OKAFOR",
    "text": "Before we start the agenda proper, I want to welcome Dr Sarah Whitfield, our safeguarding lead, who's come in to brief us on one live case.",
    "t_start": 8.6,
    "t_end": 18.8,
    "spans": [
      {
        "start": 53,
        "end": 71,
        "exemption": "s.40(2)",
        "surface": "Dr Sarah Whitfield",
        "source": "rule",
        "confidence": 1.0
      }
    ]
  },
  {
    "id": "cbbe2eb9",
    "speaker": "DR WHITFIELD",
    "text": "Thank you, chair. I'll keep this brief. We have a child open on the at-risk register, and I need it minuted that the NHS number is four zero zero, one two three, four five six four, so the case file stays linked correctly.",
    "t_start": 19.5,
    "t_end": 36.5,
    "spans": [
      {
        "start": 48,
        "end": 84,
        "exemption": "s.40(2)",
        "surface": "a child open on the at-risk register",
        "source": "rule",
        "confidence": 1.0
      },
      {
        "start": 131,
        "end": 180,
        "exemption": "s.38",
        "surface": "four zero zero, one two three, four five six four",
        "source": "rule",
        "confidence": 1.0
      }
    ]
  },
  {
    "id": "bc38a5aa",
    "speaker": "CLLR OKAFOR",
    "text": "Understood, that stays on the confidential log only. Right, moving to procurement. The evaluation panel has scored the tenders and the preferred bidder is Ardent Systems, with a bid of £2.4 million.",
    "t_start": 37.3,
    "t_end": 49.0,
    "spans": [
      {
        "start": 155,
        "end": 169,
        "exemption": "s.43(2)",
        "surface": "Ardent Systems",
        "source": "rule",
        "confidence": 1.0
      },
      {
        "start": 185,
        "end": 197,
        "exemption": "s.43(2)",
        "surface": "£2.4 million",
        "source": "rule",
        "confidence": 1.0
      }
    ]
  },
  {
    "id": "01f8183f",
    "speaker": "DR WHITFIELD",
    "text": "Before that's confirmed, I'd flag that the safeguarding threshold policy sitting underneath this contract is still in formulation, so please don't treat it as final.",
    "t_start": 49.8,
    "t_end": 59.0,
    "spans": [
      {
        "start": 106,
        "end": 129,
        "exemption": "s.35",
        "surface": "is still in formulation",
        "source": "model",
        "confidence": 0.82
      }
    ]
  },
  {
    "id": "ce43f9dd",
    "speaker": "CLLR OKAFOR",
    "text": "Fair point, we'll caveat that in the minutes. On the contract itself, legal advice from the council's solicitor came back yesterday on the indemnity clause, and it's fine to proceed.",
    "t_start": 59.8,
    "t_end": 70.5,
    "spans": [
      {
        "start": 70,
        "end": 111,
        "exemption": "s.42",
        "surface": "legal advice from the council's solicitor",
        "source": "rule",
        "confidence": 1.0
      }
    ]
  },
  {
    "id": "85467ded",
    "speaker": "CLLR OKAFOR",
    "text": "So to summarise for the minute: Dr Sarah Whitfield's referral, NHS number four zero zero, one two three, four five six four, sits alongside the Ardent Systems award at £2.4 million, and both items need the redaction applied before release.",
    "t_start": 71.3,
    "t_end": 87.0,
    "spans": [
      {
        "start": 32,
        "end": 61,
        "exemption": "s.40(2)",
        "surface": "Dr Sarah Whitfield's referral",
        "source": "rule",
        "confidence": 1.0
      },
      {
        "start": 74,
        "end": 123,
        "exemption": "s.38",
        "surface": "four zero zero, one two three, four five six four",
        "source": "rule",
        "confidence": 1.0
      },
      {
        "start": 144,
        "end": 158,
        "exemption": "s.43(2)",
        "surface": "Ardent Systems",
        "source": "rule",
        "confidence": 1.0
      },
      {
        "start": 168,
        "end": 180,
        "exemption": "s.43(2)",
        "surface": "£2.4 million",
        "source": "rule",
        "confidence": 1.0
      }
    ]
  },
  {
    "id": "31b36496",
    "speaker": "DR WHITFIELD",
    "text": "Agreed, chair. I'll send the full case summary to you separately after this.",
    "t_start": 87.8,
    "t_end": 92.5,
    "spans": []
  }
]
