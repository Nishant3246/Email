from flask import Flask, render_template, request, jsonify
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
import os
import base64
import pandas as pd
import logging
import uuid
import re
from PIL import Image
import io

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB limit
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('email_sender.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def generate_unique_filename(filename):
    """Generate a unique filename to prevent overwriting."""
    unique_id = uuid.uuid4().hex[:8]
    name, ext = os.path.splitext(filename)
    return f"{name}_{unique_id}{ext}"

def validate_excel_file(file):
    """Validate Excel file type and extension."""
    allowed_extensions = {'.xlsx', '.xls'}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions:
        return False
    
    return True

def compress_image(image_data, max_size_kb=500, quality=85):
    """
    Compress an image to reduce file size while maintaining reasonable quality.
    
    :param image_data: Base64 encoded image data
    :param max_size_kb: Maximum file size in kilobytes
    :param quality: JPEG compression quality (85 is a good balance)
    :return: Compressed base64 encoded image data
    """
    try:
        # Decode the base64 image
        image_bytes = base64.b64decode(image_data)
        
        # Open the image
        img = Image.open(io.BytesIO(image_bytes))
        
        # Convert to RGB if necessary (handles PNG with transparency)
        if img.mode in ('RGBA', 'LA'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        
        # Resize the image to a maximum width of 600 pixels while maintaining aspect ratio
        max_width = 600
        if img.width > max_width:
            height = int(img.height * max_width / img.width)
            img = img.resize((max_width, height), resample=Image.BICUBIC)
        
        # Compress the image
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=quality, optimize=True)
        compressed_size = output.tell()
        
        # If still too large, reduce quality
        while compressed_size > max_size_kb * 1024 and quality > 10:
            output = io.BytesIO()
            quality -= 5
            img.save(output, format='JPEG', quality=quality, optimize=True)
            compressed_size = output.tell()
        
        # Encode back to base64
        compressed_image = base64.b64encode(output.getvalue()).decode('utf-8')
        return f"data:image/jpeg;base64,{compressed_image}"
    
    except Exception as e:
        logger.error(f"Image compression error: {e}")
        return image_data  # Return original if compression fails

def process_inline_images_and_attachments(body, image_files=None, attachment_files=None):
    """
    Process inline images and file attachments for email.
    
    :param body: HTML email body with base64 encoded images
    :param image_files: List of image files to attach
    :param attachment_files: List of attachment files to attach
    :return: Tuple of processed body and list of attachments
    """
    image_pattern = r'src="data:image/(\w+);base64,([^"]+)"'
    processed_body = body
    email_attachments = []

    # Process inline images
    inline_image_count = 0
    for match in re.findall(image_pattern, body):
        try:
            image_type, image_data = match
            
            # Compress the image
            compressed_image_data = compress_image(image_data)
            
            # Create MIMEImage and attach it to the email
            inline_image_count += 1
            cid = f"image{inline_image_count}@example.com"
            mime_image = MIMEImage(base64.b64decode(compressed_image_data.split(',')[-1]), _subtype=image_type)
            mime_image.add_header('Content-ID', f'<{cid}>')
            processed_body = processed_body.replace(
                f'src="data:image/{image_type};base64,{image_data}"',
                f'src="cid:{cid}"'
            )

            email_attachments.append(mime_image)
        
        except Exception as e:
            logger.error(f"Error processing inline image: {e}")

    # Process uploaded image files
    if image_files:
        for file in image_files:
            try:
                # Compress the image file
                with Image.open(file) as img:
                    # Convert to RGB if necessary
                    if img.mode in ('RGBA', 'LA'):
                        background = Image.new('RGB', img.size, (255, 255, 255))
                        background.paste(img, mask=img.split()[-1])
                        img = background
                    
                    # Resize the image to a maximum width of 600 pixels while maintaining aspect ratio
                    max_width = 600
                    if img.width > max_width:
                        height = int(img.height * max_width / img.width)
                        img = img.resize((max_width, height), resample=Image.BICUBIC)
                    
                    # Save compressed image to memory
                    output = io.BytesIO()
                    img.save(output, format='JPEG', quality=85, optimize=True)
                    output.seek(0)
                    
                    # Create MIMEImage
                    mime_image = MIMEImage(output.read(), _subtype='jpeg')
                    mime_image.add_header('Content-Disposition', 'attachment', filename=file.filename)
                    email_attachments.append(mime_image)
            except Exception as e:
                logger.error(f"Error processing uploaded image file: {e}")

    # Process file attachments
    if attachment_files:
        for file in attachment_files:
            try:
                # Read file content
                file_content = file.read()
                file.seek(0)  # Reset file pointer after reading
                
                # Create MIME application for file
                mime_attachment = MIMEApplication(file_content, _subtype="octet-stream")
                mime_attachment.add_header('Content-Disposition', 'attachment', filename=file.filename)
                email_attachments.append(mime_attachment)
            except Exception as e:
                logger.error(f"Error processing attachment: {e}")


    return processed_body, email_attachments


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Validate file size
        max_size = 32 * 1024 * 1024  # 32 MB
        if request.content_length and request.content_length > max_size:
            return jsonify({
                'success': False, 
                'error': 'File too large. Maximum upload size is 32 MB.'
            }), 413

        sender_email = request.form.get("sender_email")
        app_password = request.form.get("app_password")
        subject = request.form.get("subject")
        email_body = request.form.get("email_body", "")

        # Collect image and attachment files
        uploaded_images = request.files.getlist('images')
        uploaded_attachments = request.files.getlist('attachments')

        # Initialize recipient list
        recipients = []
        if 'excel_file' in request.files:
            excel_file = request.files['excel_file']
            if excel_file and excel_file.filename != '':
                # Validate Excel file
                if not validate_excel_file(excel_file):
                    return jsonify({
                        'success': False, 
                        'error': 'Invalid file type. Please upload an Excel file (.xlsx or .xls).'
                    }), 400

                # Additional file size check
                excel_file.seek(0, os.SEEK_END)
                file_size = excel_file.tell()
                excel_file.seek(0)  # Reset file pointer

                if file_size > max_size:
                    return jsonify({
                        'success': False, 
                        'error': 'Excel file size exceeds the maximum limit of 32 MB.'
                    }), 413

                # Save the uploaded Excel file
                filename = generate_unique_filename(excel_file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                excel_file.save(filepath)

                try:
                    # Read email addresses from Excel (assume first column contains email addresses)
                    df = pd.read_excel(filepath)
                    if not df.empty:
                        recipients = df.iloc[:, 0].dropna().astype(str).tolist()
                except Exception as e:
                    logger.error(f"Error reading Excel file: {e}")
                    return jsonify({'success': False, 'error': f'Error reading Excel file: {str(e)}'})
                finally:
                    # Remove the temporary file
                    os.remove(filepath)

        # Fallback to direct input if no emails found in the file
        if not recipients:
            recipients_input = request.form.get("recipients", "")
            recipients = [email.strip() for email in re.split(r'[,\n]', recipients_input) if email.strip()]

        # Validate email addresses
        email_regex = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
        valid_recipients = [email for email in recipients if re.match(email_regex, email)]

        if not valid_recipients:
            return jsonify({'success': False, 'error': 'No valid recipients found'})

        smtp_server = "smtp.gmail.com"
        smtp_port = 587

        try:
            # Validate required fields
            if not sender_email or not app_password or not subject:
                return jsonify({'success': False, 'error': 'Missing required fields'})

            # Connect to the SMTP server
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(sender_email, app_password)
            logger.info("SMTP connection established and logged in successfully.")

            # Process inline images and attachments
            processed_body, attachments = process_inline_images_and_attachments(
                email_body, 
                image_files=uploaded_images, 
                attachment_files=uploaded_attachments
            )

            # Send emails one by one
            successful_recipients = []
            failed_recipients = []

            for recipient in valid_recipients:
                try:
                    # Prepare the email
                    msg = MIMEMultipart()
                    msg["From"] = sender_email
                    msg["To"] = recipient
                    msg["Subject"] = subject

                    # Attach processed email body
                    msg.attach(MIMEText(processed_body, "html"))

                    # Attach files and images
                    for attachment in attachments:
                        msg.attach(attachment)

                    # Send the email
                    server.sendmail(sender_email, recipient, msg.as_string())
                    successful_recipients.append(recipient)
                    logger.info(f"Email sent successfully to {recipient}")
                except smtplib.SMTPException as e:
                    failed_recipients.append((recipient, str(e)))
                    logger.error(f"Failed to send email to {recipient}: {e}")

            server.quit()

            # Response based on results
            response = {
                'success': True,
                'message': f'Emails sent to {len(successful_recipients)} recipients.',
                'successful_recipients': successful_recipients,
            }
            if failed_recipients:
                response['failed_recipients'] = [f"{r} - {e}" for r, e in failed_recipients]
                response['message'] += f" Failed to send to {len(failed_recipients)} recipients."
            return jsonify(response)

        except smtplib.SMTPAuthenticationError:
            return jsonify({'success': False, 'error': 'SMTP Authentication Error. Check your email and app password.'})
        except Exception as e:
            logger.error(f"SMTP error: {e}")
            return jsonify({'success': False, 'error': f"Error sending emails: {str(e)}"})

    return render_template("index.html")

# Error handler for large file uploads
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({
        'success': False, 
        'error': 'File size exceeds the maximum limit of 32 MB. Please upload a smaller file.'
    }), 413
    
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
