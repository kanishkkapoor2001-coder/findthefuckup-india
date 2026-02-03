# Find The Fuckup - Contract Error Checker

An AI-powered contract analysis tool that uses Claude (Anthropic's AI) to identify potential errors, inconsistencies, and problems in legal contracts.

## Features

- 📄 **Document Analysis**: Upload .docx contracts for AI-powered review
- 🔍 **Error Detection**: Identifies contradictions, ambiguities, missing definitions, and more
- 🎯 **Severity Ratings**: Issues are classified as high, medium, or low severity
- 🔗 **Share Issues**: Create shareable links for specific contract errors
- 📸 **Public Gallery**: Browse real contract errors found by the community
- 🤖 **Powered by Claude**: Uses Anthropic's Claude Sonnet 4 for analysis
- 🔒 **Security**: reCAPTCHA v3 protection, rate limiting, and corporate email validation

## Tech Stack

- **Backend**: Flask (Python)
- **Frontend**: React (via CDN)
- **AI**: Anthropic Claude API
- **Database**: PostgreSQL
- **Deployment**: Render/Heroku compatible (uses gunicorn)

## Prerequisites

- Python 3.8+
- PostgreSQL database (optional, for email tracking and gallery features)
- Anthropic API key
- Google reCAPTCHA v3 site key and secret key

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/contract-checker.git
cd contract-checker
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Set Up Environment Variables

Create a `.env` file in the root directory:

```bash
# Required
ANTHROPIC_API_KEY=your_anthropic_api_key_here
RECAPTCHA_SECRET_KEY=your_recaptcha_secret_key_here

# Optional (for database features)
DATABASE_URL=postgresql://user:password@localhost:5432/dbname
```

### 4. Configure reCAPTCHA

1. Go to [Google reCAPTCHA Admin](https://www.google.com/recaptcha/admin)
2. Register a new site with reCAPTCHA v3
3. Add your domain(s) to the allowed domains
4. Copy your site key and secret key
5. Update `index.html` line 12 with your site key:
   ```html
   <script src="https://www.google.com/recaptcha/api.js?render=YOUR_SITE_KEY_HERE"></script>
   ```
6. Update `index.html` line 663 with your site key:
   ```javascript
   const recaptchaToken = await window.grecaptcha.execute('YOUR_SITE_KEY_HERE', {action: 'submit'});
   ```
7. Add your secret key to environment variables

### 5. Set Up Database (Optional)

If you want email tracking and gallery features:

```bash
# Connect to PostgreSQL
psql -U your_username -d your_database

# Tables will be created automatically on first run
# Or you can run the initialization manually by starting the app
```

### 6. Update CORS Origins

In `app.py`, update the CORS configuration with your actual domain:

```python
CORS(app, origins=[
    "https://yourdomain.com",
    "https://www.yourdomain.com",
    "http://localhost:3000",
    "http://localhost:5000"
])
```

## Running Locally

### Development Mode

```bash
python app.py
```

The app will run on `http://localhost:5000`

### Production Mode (with gunicorn)

```bash
gunicorn app:app
```

## Deployment

### Deploy to Render

1. Create a new Web Service on [Render](https://render.com)
2. Connect your GitHub repository
3. Configure environment variables:
   - `ANTHROPIC_API_KEY`
   - `RECAPTCHA_SECRET_KEY`
   - `DATABASE_URL` (Render can auto-provision PostgreSQL)
4. Deploy!

### Deploy to Heroku

1. Install the [Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli)
2. Create a new Heroku app:
   ```bash
   heroku create your-app-name
   ```
3. Add PostgreSQL addon:
   ```bash
   heroku addons:create heroku-postgresql:mini
   ```
4. Set environment variables:
   ```bash
   heroku config:set ANTHROPIC_API_KEY=your_key_here
   heroku config:set RECAPTCHA_SECRET_KEY=your_key_here
   ```
5. Deploy:
   ```bash
   git push heroku main
   ```

## Project Structure

```
.
├── app.py              # Flask backend application
├── index.html          # React frontend (single page)
├── requirements.txt    # Python dependencies
├── Procfile           # Deployment configuration
└── README.md          # This file
```

## API Endpoints

### POST `/api/check-document`
Analyzes a document for contract errors.

**Request**: 
- `multipart/form-data`
- Fields: `document` (file), `email` (string), `recaptcha_token` (string)

**Response**:
```json
{
  "success": true,
  "summary": "Brief assessment",
  "issues": [
    {
      "paragraphIndex": 0,
      "issue": "Description",
      "suggestion": "Fix suggestion",
      "severity": "high|medium|low"
    }
  ],
  "paragraphs": [...]
}
```

### POST `/api/share-issue`
Creates a shareable link for an issue.

**Request**:
```json
{
  "paragraphIndex": 0,
  "issue": "Description",
  "suggestion": "Fix",
  "severity": "high"
}
```

**Response**:
```json
{
  "success": true,
  "shareUrl": "https://yourdomain.com/issue/uuid",
  "shareId": "uuid"
}
```

### GET `/api/gallery?page=1&per_page=20`
Retrieves paginated list of shared issues.

**Response**:
```json
{
  "success": true,
  "issues": [...],
  "page": 1,
  "perPage": 20,
  "total": 100,
  "totalPages": 5
}
```

### GET `/issue/<share_id>`
Displays a single shared issue.

### GET `/gallery`
Public gallery page.

## Configuration

### Rate Limiting

Default limits (in `app.py`):
- 200 requests per day
- 50 requests per hour
- 5 document checks per hour

Adjust in the `@limiter.limit()` decorators.

### File Size Limits

Default: 10MB maximum file size

Adjust in `app.py`:
```python
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024
```

### Email Validation

The app blocks common free email providers. To modify the list, edit the `blocked_domains` array in the `validate_email()` function in `app.py`.

### Claude Model Configuration

The app uses `claude-sonnet-4-20250514`. To change models, update the model name in the `check_document()` function.

## Security Considerations

1. **API Keys**: Never commit API keys to version control. Use environment variables.
2. **reCAPTCHA**: Properly configure reCAPTCHA to prevent abuse.
3. **Rate Limiting**: Adjust rate limits based on your usage patterns.
4. **CORS**: Restrict to your specific domain(s) in production.
5. **Input Validation**: The app validates file types, sizes, and email formats.
6. **Database**: Use SSL connections for production databases.

## Customization

### Branding

1. Update the title and taglines in `index.html`
2. Modify CSS variables for colors and fonts
3. Replace links in the footer

### Analysis Prompt

To modify what Claude looks for, edit the `prompt` variable in the `check_document()` function in `app.py`.

### Styling

All styles are in the `<style>` tag in `index.html`. The design uses:
- Font: Lora (serif) and Playfair Display
- Color scheme: Dark blue gradient background
- Mobile responsive design

## Troubleshooting

### "ANTHROPIC_API_KEY environment variable must be set"
- Make sure you've set the `ANTHROPIC_API_KEY` environment variable
- Check your `.env` file or deployment platform settings

### "reCAPTCHA verification failed"
- Verify your reCAPTCHA site key is correct in `index.html`
- Ensure your secret key is correct in environment variables
- Check that your domain is registered in Google reCAPTCHA admin

### Database connection errors
- Verify `DATABASE_URL` is correct
- Ensure PostgreSQL is running
- Check database credentials and permissions

### Rate limit errors
- Wait for the rate limit window to reset
- Adjust rate limits in `app.py` if needed
- Consider implementing Redis for distributed rate limiting

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

MIT License - feel free to use this project for any purpose.

## Acknowledgments

- Powered by [Anthropic's Claude](https://www.anthropic.com/claude)
- Built with Flask and React
- Inspired by the need for better contract review tools

## Support

For issues, questions, or contributions, please open an issue on GitHub.

## Roadmap

- [ ] Support for PDF files
- [ ] Multi-language support
- [ ] Batch document processing
- [ ] Document comparison tool
- [ ] Export analysis reports
- [ ] Custom analysis templates
- [ ] Integration with document management systems

---

**Disclaimer**: This tool is not a substitute for professional legal advice. It's designed to help identify potential issues, but should not be relied upon as the sole means of contract review. Always consult with a qualified attorney for legal matters.
