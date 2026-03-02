const nodemailer = require('nodemailer');
require('dotenv').config();

function getArgValue(flag) {
  const idx = process.argv.indexOf(flag);
  if (idx === -1 || idx + 1 >= process.argv.length) return '';
  return process.argv[idx + 1];
}

const to = getArgValue('--to');
const subject = getArgValue('--subject');
const text = getArgValue('--text');
const html = getArgValue('--html');

if (!to || !subject || (!text && !html)) {
  console.error('Missing required arguments. Usage: node mailer.js --to <email> --subject <subject> --text <message> [--html <html>]');
  process.exit(1);
}

const user = process.env.EMAIL_USER;
const pass = process.env.EMAIL_PASS;
const from = process.env.EMAIL_FROM || user;

if (!user || !pass) {
  console.error('EMAIL_USER and EMAIL_PASS must be set in environment');
  process.exit(1);
}

const transporter = nodemailer.createTransport({
  service: 'gmail',
  auth: {
    user,
    pass,
  },
});

const mailOptions = {
  from,
  to,
  subject,
};

if (html) {
  mailOptions.html = html;
  mailOptions.text = text || '';
} else {
  mailOptions.text = text;
}

transporter.sendMail(mailOptions)
  .then(() => {
    process.exit(0);
  })
  .catch((err) => {
    console.error(err && err.message ? err.message : err);
    process.exit(1);
  });
