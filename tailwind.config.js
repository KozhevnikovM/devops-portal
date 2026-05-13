/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/presentation/templates/**/*.html"],
  safelist: [
    "status-PENDING",
    "status-PROVISIONING",
    "status-RETRY",
    "status-READY",
    "status-FAILED",
  ],
  theme: { extend: {} },
  plugins: [],
}
