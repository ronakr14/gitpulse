import resend

resend.api_key = "re_be4V2qfB_Kpb2rYY2Nq7vaBUZytreTiU5"

params: resend.Emails.SendParams = {
    "from": "Acme <notification@resend.dev>",
    "to": ["delivered@resend.dev"],
    "subject": "hello world",
    "html": "<p>it works!</p>"
}

email = resend.Emails.send(params)
print(email)