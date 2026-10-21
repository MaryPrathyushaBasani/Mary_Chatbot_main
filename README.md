# Mary Chatbot Main

This repository contains the standalone MARY Employee Insights Assistant app, its query and report logic, and its web interface. It does not contain employee data, database credentials, or API keys.

## Run locally

Use Python 3.11 or newer. From this folder, create and activate a virtual environment, then install the dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Configure the required values as environment variables or in an untracked `.env` file. Never commit credentials.

| Variable | Purpose |
| --- | --- |
| `APP_ACCESS_USERNAME` | Login username |
| `APP_ACCESS_PASSWORD` | Login password |
| `MYSQL_HOST` | MySQL server hostname |
| `MYSQL_PORT` | MySQL port, normally `3306` |
| `MYSQL_USER` | Read-only database user |
| `MYSQL_PASSWORD` | Database password |
| `MYSQL_SCHEMA` | Schema containing `employees_messy` |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI key |
| `AZURE_OPENAI_API_VERSION` | Azure OpenAI API version |
| `AZURE_OPENAI_DEPLOYMENT_MINI` | Chat deployment name |

Start the app and open `http://127.0.0.1:5018/`:

```powershell
python app.py
```

The app uses the configured MySQL database and requires authentication for every route except `/healthz`. Keep the database private, use a read-only database account, and do not add database dumps, logs, or secrets to this public repository. Report and query context is stored in process memory and is cleared when the app restarts.