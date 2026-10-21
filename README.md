# Mary Chatbot Main

This folder is a self-contained copy of MARY and the Employee Insights Assistant. It includes the Flask app, query and report logic, templates, static assets, and a Dockerfile. No database or credentials are bundled.

## Required configuration

Set these as environment variables or hosting-provider secrets. Do not commit a `.env` file or place secret values in source control.

| Variable | Purpose |
| --- | --- |
| `APP_ACCESS_USERNAME` | Username required to access the app |
| `APP_ACCESS_PASSWORD` | Password required to access the app |
| `MYSQL_HOST` | MySQL hostname reachable from the app host |
| `MYSQL_PORT` | MySQL port, normally `3306` |
| `MYSQL_USER` | Dedicated, read-only database user |
| `MYSQL_PASSWORD` | Database user password |
| `MYSQL_SCHEMA` | Database/schema containing `employees_messy` |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource endpoint |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI key |
| `AZURE_OPENAI_API_VERSION` | Azure OpenAI API version |
| `AZURE_OPENAI_DEPLOYMENT_MINI` | Chat deployment name |
| `MYSQL_SSL_CA` | Optional path to the MySQL CA certificate |

SMTP settings are optional and only needed for email delivery: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_USE_TLS`, and `SMTP_USE_SSL`.

## Deployment

No hosting provider is selected yet. The container listens on `PORT` (default `8080`) and exposes `/healthz` for health checks. Configure the required variables through the selected host's secret manager; do not commit `.env` files, credentials, database dumps, or logs.

Choose a host that can run the Docker image and securely reach the MySQL database. Keep MySQL private, use TLS where supported, and grant the app's database account read-only access to the required schema/table. Keep `APP_ACCESS_USERNAME` and `APP_ACCESS_PASSWORD` set; the app rejects non-health requests if either is missing. Use HTTPS and restrict access to approved peers.

## Database and privacy

Do not expose MySQL to the public internet. Use TLS where supported by the database, and a database account with read-only grants for the required schema/table. Do not put a database dump in this repository or container image.

The app keeps report and query context in process memory. Keep a single app replica; report links and context are lost when the instance restarts. Multiple replicas require moving this state to shared storage first.