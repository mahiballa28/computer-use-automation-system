"""Meridian Credit Union — Mock Legacy Core Banking Application.

A deliberately "legacy" web application that simulates a credit union's internal
back-office system. Used as the target surface for CUA discovery and replay.

Design choices that make this a realistic proxy for legacy banking software:
- Table-based layouts (no CSS grid/flexbox)
- Inline styles instead of CSS classes
- No data-testid or aria attributes (the real world doesn't have them)
- Deeply nested tables for form layout
- Server-rendered forms with full page reloads
- Generic input names ('field1', 'field2') instead of semantic ones
- Session-based state with timeout simulation
- Injectable error states via query parameters

Routes:
  /                    → Login page
  /dashboard           → Main menu
  /members/search      → Member search (GET with query param)
  /members/<id>        → Member detail with accounts
  /members/<id>/transfer       → Transfer form (step 1)
  /members/<id>/transfer/confirm → Confirm transfer (step 2)
  /members/<id>/transfer/complete → Transfer success (step 3)
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from flask import Flask, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = "mock-bank-dev-key-not-real"


# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------


@dataclass
class Account:
    account_id: str
    account_type: str
    balance: float
    status: str = "Active"


@dataclass
class Member:
    member_id: str
    first_name: str
    last_name: str
    ssn_last4: str
    phone: str
    email: str
    address: str
    accounts: list[Account] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


MEMBERS: dict[str, Member] = {
    "M1001": Member(
        member_id="M1001",
        first_name="Alice",
        last_name="Johnson",
        ssn_last4="4532",
        phone="(808) 555-0101",
        email="alice.johnson@email.com",
        address="123 Main St, Honolulu, HI 96801",
        accounts=[
            Account("CHK-1001", "Checking", 2_845.67),
            Account("SAV-1001", "Savings", 15_230.89),
            Account("CD-1001", "Certificate of Deposit", 50_000.00, "Locked"),
        ],
    ),
    "M1002": Member(
        member_id="M1002",
        first_name="Robert",
        last_name="Chen",
        ssn_last4="7891",
        phone="(808) 555-0102",
        email="robert.chen@email.com",
        address="456 King St, Honolulu, HI 96813",
        accounts=[
            Account("CHK-1002", "Checking", 567.23),
            Account("SAV-1002", "Savings", 3_100.00),
        ],
    ),
    "M1003": Member(
        member_id="M1003",
        first_name="Maria",
        last_name="Santos",
        ssn_last4="2345",
        phone="(808) 555-0103",
        email="maria.santos@email.com",
        address="789 Ala Moana Blvd, Honolulu, HI 96814",
        accounts=[
            Account("CHK-1003", "Checking", 12_456.78),
            Account("SAV-1003", "Savings", 89_100.50),
            Account("SAV-1003B", "Savings (Joint)", 5_200.00),
        ],
    ),
}


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def check_login() -> bool:
    return session.get("logged_in", False)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def login_page():
    error = request.args.get("error", "")
    return render_template("login.html", error=error)


@app.route("/login", methods=["POST"])
def do_login():
    username = request.form.get("username", "")
    if username:
        session["logged_in"] = True
        session["username"] = username
        session["login_time"] = time.time()
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page", error="Username is required"))


@app.route("/dashboard")
def dashboard():
    if not check_login():
        return redirect(url_for("login_page", error="Session expired. Please log in again."))

    # Simulate session timeout via query param (for testing)
    if request.args.get("simulate_timeout"):
        session.clear()
        return redirect(url_for("login_page", error="Session expired. Please log in again."))

    return render_template("dashboard.html", username=session.get("username", "operator"))


@app.route("/members/search")
def member_search():
    if not check_login():
        return redirect(url_for("login_page", error="Session expired"))

    query = request.args.get("q", "").strip()
    results: list[Member] = []

    if query:
        # Simulate slow search via query param
        if request.args.get("simulate_slow"):
            time.sleep(3)

        for member in MEMBERS.values():
            if (
                query.upper() in member.member_id.upper()
                or query.lower() in member.first_name.lower()
                or query.lower() in member.last_name.lower()
                or query.lower() in member.full_name.lower()
            ):
                results.append(member)

    return render_template("search_results.html", query=query, results=results)


@app.route("/members/<member_id>")
def member_detail(member_id: str):
    if not check_login():
        return redirect(url_for("login_page", error="Session expired"))

    member = MEMBERS.get(member_id)
    if not member:
        return render_template("error.html", error_title="Member Not Found",
                             error_message=f"No member found with ID: {member_id}",
                             error_code="ERR_MEMBER_NOT_FOUND"), 404

    # Simulate permission denial
    if request.args.get("simulate_permission_denied"):
        return render_template("error.html", error_title="Access Denied",
                             error_message="You do not have permission to view this member's information.",
                             error_code="ERR_PERMISSION_DENIED"), 403

    return render_template("member_detail.html", member=member)


@app.route("/members/<member_id>/transfer", methods=["GET", "POST"])
def transfer_form(member_id: str):
    if not check_login():
        return redirect(url_for("login_page", error="Session expired"))

    member = MEMBERS.get(member_id)
    if not member:
        return render_template("error.html", error_title="Member Not Found",
                             error_message=f"No member found with ID: {member_id}",
                             error_code="ERR_MEMBER_NOT_FOUND"), 404

    error = ""
    if request.method == "POST":
        from_account = request.form.get("from_acct", "")
        to_account = request.form.get("to_acct", "")
        amount_str = request.form.get("amount", "")

        # Validation
        if not from_account or not to_account:
            error = "Both source and destination accounts are required."
        elif from_account == to_account:
            error = "Source and destination accounts must be different."
        elif not amount_str:
            error = "Transfer amount is required."
        else:
            try:
                amount = float(amount_str)
                if amount <= 0:
                    error = "Amount must be greater than zero."
                elif amount > 100_000:
                    error = "Amount exceeds single transfer limit of $100,000."
                else:
                    # Find source account and check balance
                    src = next((a for a in member.accounts if a.account_id == from_account), None)
                    if src and amount > src.balance:
                        error = f"Insufficient funds. Available balance: ${src.balance:,.2f}"
                    else:
                        # Store transfer details in session for confirmation
                        session["pending_transfer"] = {
                            "member_id": member_id,
                            "from_account": from_account,
                            "to_account": to_account,
                            "amount": amount,
                        }
                        return redirect(url_for("transfer_confirm", member_id=member_id))
            except ValueError:
                error = "Invalid amount format."

    return render_template("transfer_form.html", member=member, error=error)


@app.route("/members/<member_id>/transfer/confirm", methods=["GET", "POST"])
def transfer_confirm(member_id: str):
    if not check_login():
        return redirect(url_for("login_page", error="Session expired"))

    transfer = session.get("pending_transfer")
    if not transfer or transfer["member_id"] != member_id:
        return redirect(url_for("transfer_form", member_id=member_id))

    member = MEMBERS.get(member_id)
    if not member:
        return render_template("error.html", error_title="Member Not Found",
                             error_message=f"No member found with ID: {member_id}",
                             error_code="ERR_MEMBER_NOT_FOUND"), 404

    if request.method == "POST":
        # Simulate random processing error
        if request.args.get("simulate_error"):
            return render_template("error.html", error_title="Processing Error",
                                 error_message="Transaction could not be completed. Please try again.",
                                 error_code="ERR_PROCESSING"), 500

        # "Process" the transfer
        conf_number = f"TXN-{random.randint(100_000, 999_999)}"
        session["last_confirmation"] = conf_number
        session.pop("pending_transfer", None)
        return redirect(url_for("transfer_complete", member_id=member_id))

    from_acct = next((a for a in member.accounts if a.account_id == transfer["from_account"]), None)
    to_acct = next((a for a in member.accounts if a.account_id == transfer["to_account"]), None)

    return render_template(
        "transfer_confirm.html",
        member=member,
        transfer=transfer,
        from_acct=from_acct,
        to_acct=to_acct,
    )


@app.route("/members/<member_id>/transfer/complete")
def transfer_complete(member_id: str):
    if not check_login():
        return redirect(url_for("login_page", error="Session expired"))

    conf_number = session.get("last_confirmation", "N/A")
    member = MEMBERS.get(member_id)

    return render_template("transfer_complete.html", member=member,
                         confirmation_number=conf_number)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
