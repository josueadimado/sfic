# Set Free In Christ (Django MVP)

This project is a Django implementation of the Freedom Intensive registration website with:

- one-page public landing page
- registration + Stripe checkout
- Stripe webhook payment confirmation
- custom admin dashboard with filters and CSV export

## Quick Start

1. Create and activate your virtual environment:

   - macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate`

2. Install dependencies:

   - `pip install -r requirements.txt`

3. Configure environment variables:

   - `cp .env.example .env`
   - add your Stripe keys to `.env`

4. Run migrations:

   - `python manage.py migrate`

5. Create admin user:

   - `python manage.py createsuperuser`

6. (Optional) Load sample sessions:

   - `python manage.py loaddata intensive/fixtures/sample_sessions.json`
   - `python manage.py loaddata intensive/fixtures/sample_schedule.json`
   - `python manage.py loaddata intensive/fixtures/sample_speakers.json`

7. Start server:

   - `python manage.py runserver`

## Main URLs

- `/` public page
- `/success` payment success page
- `/cancel` payment cancel page
- `/dashboard/login/` custom dashboard login
- `/dashboard/` custom admin dashboard
- `/admin/` Django admin
- `/webhooks/stripe` Stripe webhook endpoint
