import urllib.parse
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

# 1. Twilio Dashboard se copy ki hui details
ACCOUNT_SID = "AC2fe0b48930451fe314530fd5f997c3a6"
AUTH_TOKEN = "5ba0241eeb403ffd08c43c3314be3ced"
TWILIO_NUMBER = "+17372508034"  # Twilio ka virtual number

# 2. Aapka verified mobile number (Country code ke sath, jaise +91...)
MY_PHONE_NUMBER = "+917389652540"

def make_call():
    client = Client(ACCOUNT_SID, AUTH_TOKEN)

    # 3. Jo report agent bolega
    report_text = (
        "Namaste. Yeh aapka AI voice report agent hai. "
        "Aaj aapki sabhi automated tasks successfully complete ho chuki hain. "
        "Shukriya."
    )

    # 4. Voice setup (Aditi voice Hindi ke liye)
    response = VoiceResponse()
    response.say(report_text, voice="Polly.Aditi", language="hi-IN")

    # 5. Twilio XML URL generate karna
    encoded_twiml = urllib.parse.quote(str(response))
    twiml_url = f"http://twimlets.com/echo?Twiml={encoded_twiml}"

    # 6. Call lagana
    print("Call lagayi ja rahi hai...")
    call = client.calls.create(
        to=MY_PHONE_NUMBER,
        from_=TWILIO_NUMBER,
        url=twiml_url
    )

    print(f"Call trigger ho chuki hai! Call SID: {call.sid}")

if __name__ == "__main__":
    make_call()