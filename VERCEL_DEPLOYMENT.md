# Deploying Resume Shortlisting AI on Vercel

This guide explains how to deploy your AI platform to Vercel for both the Python (Flask) backend and the frontend.

## 🚀 One-Click Deployment Requirements

1.  **Vercel Account**: Sign up at [vercel.com](https://vercel.com).
2.  **Vercel CLI**: (Optional) Install via `npm install -g vercel`.
3.  **Environment Variables**: You MUST set these in the Vercel dashboard.

## 📁 Deployment Files Created

The following files have been added to your project to enable Vercel support:

*   [`vercel.json`](file:///c:/Users/aryan/Videos/Resume%20Shortlisting%20Using%20AI/vercel.json): Configures the Python runtime and routing.
*   [`.vercelignore`](file:///c:/Users/aryan/Videos/Resume%20Shortlisting%20Using%20AI/.vercelignore): Optimizes the deployment package by excluding unnecessary files.
*   **Python Mailer**: I replaced the Node.js mailer in `app.py` with a native Python implementation. This is critical because Vercel serverless functions (Python) do not have access to a separate Node.js environment via `subprocess`.

## 🛠️ Step-by-Step Deployment

### 1. Connect to GitHub (Recommended)
1.  Push your code to a GitHub repository.
2.  Go to the [Vercel Dashboard](https://vercel.com/dashboard) and click **"Add New..."** > **"Project"**.
3.  Import your repository.

### 2. Configure Environment Variables
In the **"Environment Variables"** section during setup, add the following (copy from your local `.env`):

| Variable | Description |
| :--- | :--- |
| `MONGO_URI` | Your MongoDB Atlas connection string. |
| `RAPIDAPI_KEY` | Your RapidAPI key for resume parsing. |
| `RESUME_MATCHER_API_KEY` | API key for matching. |
| `SKILLS_PARSER_API_KEY` | API key for skill extraction. |
| `RESUME_MATCHER_HOST` | `resume-matcher-api.p.rapidapi.com` |
| `SKILLS_PARSER_HOST` | `skills-parser1.p.rapidapi.com` |
| `EMAIL_USER` | Your email for sending notifications. |
| `EMAIL_PASS` | Your email app password. |
| `EMAIL_FROM` | The sender email address. |

### 3. Build & Deploy
1.  Vercel will automatically detect the Python environment.
2.  Click **"Deploy"**.
3.  Wait for the build to finish. Vercel will install the requirements from `requirements.txt`.

## ⚠️ Important Considerations for Serverless

*   **Ephemeral Storage**: The `uploads/` folder is NOT persistent on Vercel. Resumes uploaded will only exist during that session. For production, consider using AWS S3 or Google Cloud Storage.
*   **MongoDB Atlas Permission**: Ensure your MongoDB Atlas cluster allows connections from your Vercel deployment IP (recommended: whitelist `0.0.0.0/0` in Atlas Network Access).
*   **Memory Limit**: ML libraries (scikit-learn) can sometimes be memory-intensive. If you encounter errors, you may need to implement a more lightweight matching algorithm or upgrade your Vercel plan.

## 💻 Local Testing with Vercel CLI
To test how it will look on Vercel locally:
```bash
vercel dev
```
This will start a local server that simulates the Vercel environment.
