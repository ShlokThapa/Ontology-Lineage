"""The questions offered on the chat's start screen.

They are written the way a business owner would ask, and they are deliberately demanding: each one needs several
tools (data, policy text, lineage). That is slow, so their answers are prepared in advance and kept in
``cached_answers.json`` next to this file (see ``answer_cache.py``). Rebuild that file with
``backend\\.venv\\Scripts\\python scripts\\build_chat_cache.py`` after the data, the policies or this list change.
"""

from __future__ import annotations

QUESTIONS: list[dict[str, str]] = [
    {
        "id": "data-health",
        "label": "Data health check",
        "question": "Give me a health check of our banking data. Where is the quality weakest, and which problems should we fix first?",
    },
    {
        "id": "kyc-gaps",
        "label": "KYC compliance gaps",
        "question": "Which active accounts belong to customers whose KYC has expired or is still pending, and what does our policy say about serving them?",
    },
    {
        "id": "loan-risk-exceptions",
        "label": "Loan risk exceptions",
        "question": "Are our non-performing loans being classified as high risk, as our credit risk policy requires? Give me the exceptions and what the policy says.",
    },
    {
        "id": "branch-risk",
        "label": "Risk by branch",
        "question": "Which branches hold the most high-risk loans, and how much is still outstanding on them compared with the rest of the loan book?",
    },
    {
        "id": "customer-protection",
        "label": "Customer data protection",
        "question": "Which customer identity and contact details are masked before they reach our reporting layer, and does that meet what our data protection policy requires?",
    },
    {
        "id": "change-impact",
        "label": "Impact of a change",
        "question": "If the structure of the customer table changed tomorrow, which downstream tables, pipelines and policies would be affected?",
    },
    {
        "id": "dormant-money",
        "label": "Dormant accounts",
        "question": "How much money sits in dormant accounts, which customer segments hold it, and what does our retail banking policy say about dormant accounts?",
    },
    {
        "id": "trust-risk-rating",
        "label": "Can we trust a risk rating?",
        "question": "Where does a loan's risk rating come from, and how far can we trust it from the source system to the final report?",
    },
]
