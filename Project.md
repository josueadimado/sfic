# Freedom Intensive — Registration & Seat Reservation (One-Page App)

Minimalist one-page landing + registration + payment flow to reserve seats for upcoming sessions of the “3 Day Freedom Intensive”.

---

## 1) Goal

Build a fast, clean, mobile-first single-page site where users can:

1) Understand the event quickly  
2) Select a session date  
3) Register (name/email/phone + optional fields)  
4) Pay to reserve a seat  
5) Receive confirmation (email + success page)  
6) Admin can view/export registrations

---

## 2) Tech Stack (Suggested)

### Option A (Recommended for speed)
- Frontend: Next.js (App Router) + Tailwind CSS
- Backend: Next.js API routes
- DB: PostgreSQL (Supabase) or SQLite (dev) -> Postgres (prod)
- Payments: Stripe OR Paystack (choose 1 based on client needs)
- Email: Resend / SendGrid / SMTP

### Option B (Super simple MVP)
- Frontend: Next.js + Tailwind
- Payments: payment link redirect (Stripe Checkout / Paystack Checkout)
- Registrations stored: Supabase table
- Email: Supabase Edge Function or Resend

---

## 3) User Flow

### Landing → Register → Pay → Confirm
1. User reads sections (Hero → About → What’s Included → Schedule → Sessions).
2. User clicks “Register Now” (scrolls to form).
3. User fills form + chooses session.
4. User clicks “Pay & Reserve Seat”.
5. Payment completes.
6. App marks registration as PAID and shows “Reservation Confirmed” page.
7. Email confirmation sent.

---

## 4) Minimal One-Page Content Structure

### Sections (in order)
1. Hero (headline + subtitle + CTA)
2. About the Intensive
3. What You Will Experience (4 bullet blocks)
4. What’s Included
5. Training Schedule
6. Upcoming Sessions (cards)
7. Registration + Payment (form)
8. Meet the Presenters (placeholder)
9. Final CTA + Footer

---

## 5) Key Requirements

### Functional
- One-page landing with anchor navigation
- Registration form + session selection
- Payment integration
- Seat reservation logic:
  - each session has a capacity
  - prevent overbooking (no more paid seats than capacity)
- Confirmation page + confirmation email
- Admin view (simple protected route) + export CSV

### Non-functional
- Mobile-first
- Loads fast (optimized images)
- Accessible (labels, contrast, keyboard navigation)
- Secure handling of payment callbacks/webhooks

---

## 6) Data Model

### Sessions
- id (string/uuid)
- title (e.g., “March 2026”)
- startDate (YYYY-MM-DD)
- endDate (YYYY-MM-DD)
- capacity (int)
- price (int, in cents or lowest currency unit)
- currency (e.g., USD)
- isActive (bool)

### Registrations
- id (string/uuid)
- fullName (string)
- email (string)
- phone (string)
- church (string, optional)
- sessionId (FK)
- status (enum: PENDING | PAID | CANCELED)
- paymentProvider (enum: STRIPE | PAYSTACK)
- paymentRef (string)  // checkout session id or paystack reference
- amountPaid (int)
- currency (string)
- createdAt, updatedAt

---

## 7) Pages / Routes

### Public
- `/` : one-page landing + register form
- `/success?ref=...` : payment success confirmation
- `/cancel` : payment canceled

### Admin (simple)
- `/admin` : list of registrations + filters + export CSV  
  - protected with password or basic auth (MVP)
  - later: proper login

---

## 8) API Endpoints

### Create Checkout
POST `/api/checkout`
Body:
{
  "sessionId": "uuid",
  "fullName": "...",
  "email": "...",
  "phone": "...",
  "church": "optional"
}

Server actions:
- validate inputs
- ensure session is active
- ensure capacity not exceeded (count PAID seats < capacity)
- create a Registration with status=PENDING
- create payment checkout
- return { checkoutUrl }

### Payment Webhook
POST `/api/webhooks/{provider}`
Server actions:
- verify signature
- locate Registration via paymentRef
- mark as PAID
- send confirmation email
- (optional) notify admin email

### Admin Export
GET `/api/admin/export?sessionId=...`
- returns CSV
- protected

---

## 9) UI Components Checklist

### Core components
- `<Navbar />` (optional minimal)
- `<Hero />` (CTA scroll to form)
- `<Section />` wrapper (spacing consistency)
- `<SessionCards />` + `<SessionCard />`
- `<RegistrationForm />`
- `<PaymentButton />`
- `<Faq />` (optional later)
- `<Footer />`

### Form fields
Required:
- Full Name
- Email
- Phone
- Session selection

Optional:
- Church/Organization

Validation:
- email format
- phone not empty (basic)
- session must be chosen

---

## 10) Seat Reservation Logic (Important)

### Rule
Only PAID registrations count toward capacity.

### Edge Case
Many people may attempt to pay at the same time.

### MVP solution
- Before creating checkout: check PAID count < capacity.
- When webhook confirms payment: re-check capacity.
  - If capacity exceeded at webhook time:
    - mark as "CANCELED" and trigger refund flow (or manual admin handling)
    - email user explaining fully booked and next steps

### Better solution (later)
- Reserve seat temporarily for 10–15 minutes (HOLD status)
- expire holds automatically (cron)

---
