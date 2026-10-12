# Run report run-ef27b642

- Company: steel
- Task: Process the supplier invoices in the inbox and tell me what was posted, held, rejected or needs approval.
- Final status: **completed**
- Decision engine: gemini (gemini-3.5-flash-lite)
- Human input: auto
- Model calls: 9 · input tokens 34696 · output 1285 · thinking 1027

## Plan
1. Sign in to application
1. List files in inbox
1. Read invoice inv_1042.pdf
1. Check vendor and PO details
1. Propose decision and process invoice

Success criteria:
- All invoices in the inbox are processed correctly according to company rules

## Outcomes (independently verified)

| Case | Action | Decided by | Verification | Final state |
|---|---|---|---|---|
| inv_1042.pdf | post | gemini | verified | post |

Overall verification: **completed**. Unexpected effects: none

## Agent proposal vs independent rules (initial proposals)

| Case | v | Agent proposal | Rules | Result | Differences |
|---|---|---|---|---|---|
| inv_1042.pdf | 1 | post | post | agree | — |

## Human decisions


## Writes

- inv_1042.pdf · steel.record_invoice /invoices · **committed** · Saved: Invoice posted to ledger. Ledger amount ₹3,68,160.00.

## Verification checks

### inv_1042.pdf — verified
- ✅ exactly one record saved for this document · 1 new record(s)
- ✅ saved vendor_id matches authorized value · saved 'V-101', authorized 'V-101'
- ✅ saved invoice_no matches authorized value · saved 'ERM/24-25/0912', authorized 'ERM/24-25/0912'
- ✅ saved po_no matches authorized value · saved 'PO-4501', authorized 'PO-4501'
- ✅ saved decision matches authorized value · saved 'post', authorized 'post'
- ✅ saved total matches authorized value · saved ₹3,68,160.00, authorized ₹3,68,160.00
- ✅ saved subtotal matches authorized value · saved ₹3,12,000.00, authorized ₹3,12,000.00
- ✅ saved tax matches authorized value · saved ₹56,160.00, authorized ₹56,160.00
- ✅ saved rate matches authorized value · saved ₹52,000.00, authorized ₹52,000.00
- ✅ saved quantity matches authorized value · saved 6.000, authorized 6.000
- ✅ saved grade matches authorized value · saved 'IS 2062 E250BR plates', authorized 'IS 2062 E250BR plates'
- ✅ record linked to this source document · saved 'inv_1042.pdf'
- ✅ posted record reconciles arithmetically 
- ✅ exactly one ledger posting · 1 posting(s)
- ✅ ledger amount equals authorized total · ledger ₹3,68,160.00, authorized ₹3,68,160.00

## Evidence screenshots

- before decision inv_1042.pdf: `shots/001-before-decision-inv-1042-pdf.png`
- write committed: `shots/002-write-committed.png`
- final state: `shots/003-final-state.png`
