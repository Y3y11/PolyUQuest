"""Pure versioned HTML content shared by in-process and container E2E fixtures."""

from __future__ import annotations


def fixture_html(token: str, version: int) -> str:
    approver = "Platform Team" if version == 1 else "Security Review Board"
    procedure_change = (
        "The Platform Team reviews the request within two business days."
        if version == 1
        else "The Security Review Board reviews the request within three business days."
    )
    overview = " ".join(
        [
            f"The {token} production database access service is the official "
            "workflow for employees.",
            "It applies to temporary and continuing access, protects customer "
            "information, and records every decision for audit.",
            "Employees must use the controlled request process instead of sharing "
            "credentials or contacting an individual administrator.",
            "The guide explains prerequisites, approval, activation, renewal, "
            "expiry, and evidence retained by the organisation.",
        ]
        * 4
    )
    procedure = " ".join(
        [
            f"Step 1: open Access Portal {token} and select Production Database Access.",
            "Step 2: submit the target system, business purpose, requested role, "
            "manager, and expiry date.",
            f"Step 3: obtain approval from {approver} before any credential is issued.",
            procedure_change,
            "Step 4: complete security training and attach the completion record to the request.",
            "Step 5: verify least-privilege access after activation and report any "
            "mismatch immediately.",
            "The request is rejected when the business purpose, owner, expiry date, "
            "or required approval is missing.",
        ]
        * 3
    )
    operations = " ".join(
        [
            "Service ownership remains with the Enterprise Access Operations group.",
            "Access expires automatically on the approved date and must be renewed "
            "through a new request.",
            "Audit events contain the request identifier, decision, reviewer, "
            "activation time, and revocation time.",
            "Incidents are reported through the standard security channel and do "
            "not bypass the access workflow.",
        ]
        * 4
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <title>{token} Production Database Access Guide</title>
  <meta name="description" content="Official request, approval, and renewal procedure.">
</head>
<body>
  <main id="content">
    <section id="overview"><h1>Production database access</h1><p>{overview}</p></section>
    <section id="procedure"><h2>Request steps and approval</h2><p>{procedure}</p></section>
    <section id="operations"><h2>Operations and audit</h2><p>{operations}</p></section>
  </main>
</body>
</html>"""
