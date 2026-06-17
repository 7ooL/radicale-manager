# Local Development

This is the generic local development workflow. Keep personal notes, machine-specific paths, and local credentials in your own ignored `LOCAL_DEV.md` and `.env`.

## First-Time Setup

1. Open the repository folder in VS Code.
2. Install the Python extension if VS Code prompts for it.
3. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env`:

   ```powershell
   Copy-Item .env.example .env
   ```

5. Edit `.env` for your local Radicale server and keep local data pointed at:

   ```text
   PROFILE_STORE_PATH=instance/local-profiles.db
   ```

6. Press `F5` in VS Code and choose `Radicale Manager: local app`.
7. Open `http://localhost:5000`.

## VS Code Tasks

The repository includes shared VS Code tasks:

- `Install dependencies`
- `Run unit tests`
- `Run local app`

Open the Command Palette and choose `Tasks: Run Task` to use them.

## Daily Commands

Run the local app:

```powershell
python app\main.py
```

Run tests:

```powershell
python -m unittest discover -s app
```

## Local Data

Use this local database path:

```text
instance/local-profiles.db
```

The `instance/` directory is ignored by git. Delete `instance/local-profiles.db` when you want a clean local profile database.

## Production Reminder

Production should use a persistent Docker volume for `/data`, because the default production database path is:

```text
/data/profiles.db
```
