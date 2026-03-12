# Irres API to Botpress Sync

This repository contains the code and workflows for syncing data from the Irres API to Botpress. This is a private repository for internal use only.

## Repository Structure

```
root/
├── api/                  # API code for syncing to Render
│   ├── sync_botpress.py   # Main sync script
│   └── .env               # Environment variables (ignored by .gitignore)
├── github-action/         # GitHub Actions workflow
│   └── .github/
│       └── workflows/
│           └── daily_sync.yml  # Workflow for daily sync
├── .gitignore            # Git ignore rules
└── README.md             # This file
```

## Setup

### Prerequisites

- Python 3.10 or higher
- GitHub account with repository access
- Render account for deployment
- Botpress account and API token

### Steps

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/your-username/irres-api-to-botpress-sync.git
   cd irres-api-to-botpress-sync
   ```

2. **Install Dependencies:**

   ```bash
   pip install requests
   ```

3. **Configure Secrets:**
   - Add `BOT_ID` and `BOTPRESS_TOKEN` to your GitHub repository secrets.
   - Ensure the `.env` file in the `api` directory contains the necessary environment variables.

## GitHub Actions

The repository includes a GitHub Actions workflow (`daily_sync.yml`) that runs daily to sync data from the Irres API to Botpress. The workflow:

- Runs on a schedule (daily at midnight).
- Uses Python 3.10.
- Passes `BOT_ID` and `BOTPRESS_TOKEN` as environment variables.
- Has a timeout of 7 minutes.

### Workflow File

The workflow is located at:

```
.github/workflows/daily_sync.yml
```

### Secrets

Ensure the following secrets are set in your GitHub repository:

- `BOT_ID`: Your Botpress bot ID.
- `BOTPRESS_TOKEN`: Your Botpress API token.

## Render Deployment

The `api` directory contains the code for deploying the sync script to Render. The `sync_botpress.py` script:

- Fetches data from the Irres API.
- Processes the data.
- Syncs the data to Botpress.

### Environment Variables

Ensure the `.env` file in the `api` directory contains the necessary environment variables:

```env
BOT_ID=your-bot-id
BOTPRESS_TOKEN=your-botpress-token
```

## Contributing

This repository is for internal use only. Please follow these steps for contributing:

1. Create a new branch (`git checkout -b feature/your-feature`).
2. Make your changes.
3. Commit your changes (`git commit -am 'Add new feature'`).
4. Push to the branch (`git push origin feature/your-feature`).
5. Open a pull request for review.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Support

For issues or questions, please contact the maintainers or open an issue in the repository.

---

**Note:** This repository is for internal use only. Do not share or distribute the code or secrets.
