# AI Concierge - Restuarant Backend

A production-ready Django application that powers an AI telephonic concierge service for restaurants, built using **Retell AI**, **Twilio**, and **Stripe**. This backend handles inbound webhook events from the AI agent, manages restaurant details and knowledge bases, tracks usage-based billing, handles SMS escalations, and provides a portal for restaurant owners to configure their agent exactly to their brand specifications.

## Features

- **Dynamic AI Configuration**: Integrates directly with Retell AI. The backend pushes dynamic variables (restaurant name, menu, address, hours, booking rules) into the agent's system prompt right as the call connects.
- **Call Analysis & Notification Pipeline**: Robustly parses Retell webhook events (`call_ended` and `call_analyzed`) to extract intent (reservations, complaints, general inquiries).
- **Automated Alerts**: Triggers real-time email alerts and daily summaries for restaurant owners.
- **Usage-Based Billing**: Integrates with Stripe to track subscription states and deduct telecommunication costs natively on a per-minute markup basis.
- **Post-Call SMS Automation**: Configurable Twilio integration that sends automated SMS follow-ups after the call ends (e.g., booking links, parking instructions).
- **Owner Dashboard Portal**: A frontend Django portal where restaurant owners can log in to update their Knowledge Base, view Call History metrics, modify Notification preferences, and update their Account settings.

---

## Architecture Overview

The application follows a standard Django architecture:
- `backend/` - The main Django project configuration.
- `restaurants/` - The core Django App encompassing models, views, forms, and tools.
- **Data Models**: `Restaurant`, `Subscription`, `RestaurantKnowledgeBase`, `CallEvent`, `CallDetail`, `SmsLog`, `PendingEmailChange`.
- **Database**: SQLite (local) / PostgreSQL (production).

### Key Workflows
1. **Inbound Webhook (`/api/retell/webhook/<id>/`)**: Called by Retell the instant a call connects. We verify the signature and return `dynamic_variables` to inject restaurant facts into the prompt.
2. **Event Webhook (`/api/retell/events/`)**: Called asynchronously when calls end or are analyzed. Handles cost deduction, database logging, transcription parsing, and email/SMS triggers.

---

## Getting Started

### Prerequisites
- Python 3.12+
- Valid API keys for **Retell AI**, **Stripe**, and optionally **Twilio** (if using SMS).
- SMTP Credentials (like Gmail) for notification emails.

### 1. Installation

Clone the repository and install the dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

### 2. Environment Variables

Create a `.env` file inside the `backend/` directory with the following variables:

```env
# Core Django
DJANGO_SECRET_KEY=your-django-secret-key-here
DEBUG=False
ALLOWED_HOSTS=localhost,127.0.0.1,your-production-domain.com

# Retell Integration
RETELL_API_KEY=your-retell-dashboard-api-key
RETELL_WEBHOOK_URL=https://your-production-domain.com
RETELL_DEV_BYPASS_SECRET=your-secret-bypass-header # for local testing

# Stripe Integration
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID=price_...
STRIPE_COMMUNICATION_PRICE_ID=price_...

# Twilio (Fallback/Platform level)
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1234567890

# Email (SMTP)
EMAIL_HOST_USER=your-email@gmail.com
EMAIL_HOST_PASSWORD=your-app-password
```

### 3. Database Setup

Apply the Django migrations and create a superuser for the admin dashboard:
```bash
cd backend
python manage.py migrate
python manage.py createsuperuser
```

### 4. Running the Server

Start the local development server:
```bash
python manage.py runserver
```

*(Note: For webhooks to work locally, you must use a tunneling service like **ngrok** to expose localhost to the public internet, and update `ALLOWED_HOSTS` and `RETELL_WEBHOOK_URL` in your `.env` file.)*

---

## Testing & Security

The application includes a rigorous set of automated tests (66 scenarios) ensuring webhook security, prompt processing, billing deductions, and user authentication constraints.

To run the entire test suite:
```bash
python manage.py test
```
