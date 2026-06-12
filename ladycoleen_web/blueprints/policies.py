from flask import Blueprint, render_template

policies_bp = Blueprint("policies", __name__)


@policies_bp.route("/policies/refund")
def refund_policy():
    return render_template("policies/policy_page.html",
        title="Refund Policy",
        subtitle="Last updated: June 2025",
        sections=[
            {
                "heading": "Custom Cakes",
                "body": """
                    <p>All custom cake orders are made to order and are non-refundable once the baking process has begun.</p>
                    <ul>
                        <li><strong>Cancellations more than 48 hours before collection/delivery:</strong> A store credit equal to the full order value will be issued, valid for 12 months.</li>
                        <li><strong>Cancellations less than 48 hours before collection/delivery:</strong> No refund or credit can be issued as ingredients and labour have already been committed.</li>
                        <li><strong>Defective or incorrect cakes:</strong> If your cake arrives damaged or significantly different from what was agreed, please contact us within 24 hours of collection/delivery with photos. We will either replace the item or issue a full refund at our discretion.</li>
                    </ul>
                """
            },
            {
                "heading": "Farm Shop Products",
                "body": """
                    <p>We take pride in the quality of all our farm-fresh products.</p>
                    <ul>
                        <li><strong>Damaged or incorrect items:</strong> If you receive a damaged or incorrect product, please contact us within 3 days of receiving your order. We will arrange a replacement or full refund.</li>
                        <li><strong>Perishable goods:</strong> Due to the perishable nature of farm products, we are unable to accept returns for change of mind. Please check your order carefully at the time of collection or delivery.</li>
                        <li><strong>Non-perishable goods:</strong> Unopened non-perishable items may be returned within 7 days of purchase for a refund or exchange.</li>
                    </ul>
                """
            },
            {
                "heading": "How to Request a Refund",
                "body": """
                    <p>To request a refund, please email us at <a href="mailto:orders@ladycoleen.co.za">orders@ladycoleen.co.za</a> with:</p>
                    <ul>
                        <li>Your order reference number</li>
                        <li>A description of the issue</li>
                        <li>Photos of the item if applicable</li>
                    </ul>
                    <p>Refunds will be processed to the original payment method within 5-7 business days of approval.</p>
                """
            },
        ]
    )


@policies_bp.route("/policies/privacy")
def privacy_policy():
    return render_template("policies/policy_page.html",
        title="Privacy Policy",
        subtitle="Last updated: June 2025",
        sections=[
            {
                "heading": "Who We Are",
                "body": """
                    <p>Lady Coleen Boutique Farmstall ("we", "our", "us") operates the website <strong>ladycoleen.co.za</strong>.
                    We are committed to protecting your personal information in accordance with the
                    <strong>Protection of Personal Information Act, 2013 (POPIA)</strong> of South Africa.</p>
                """
            },
            {
                "heading": "Information We Collect",
                "body": """
                    <p>We collect the following personal information when you use our website or place an order:</p>
                    <ul>
                        <li><strong>Name and contact details:</strong> name, email address, phone number</li>
                        <li><strong>Delivery information:</strong> physical delivery address</li>
                        <li><strong>Order history:</strong> products ordered and payment references</li>
                        <li><strong>Account information:</strong> if you register, your login credentials (password is stored securely and never readable)</li>
                    </ul>
                    <p>We do <strong>not</strong> store your card or banking details. Payments are processed securely by <strong>PayFast</strong>, a PCI-DSS compliant payment gateway.</p>
                """
            },
            {
                "heading": "How We Use Your Information",
                "body": """
                    <p>We use your personal information to:</p>
                    <ul>
                        <li>Process and fulfil your orders</li>
                        <li>Send order confirmations and updates by email</li>
                        <li>Respond to enquiries and customer service requests</li>
                        <li>Improve our products and website</li>
                    </ul>
                    <p>We will never sell or share your personal information with third parties for marketing purposes.</p>
                """
            },
            {
                "heading": "Third-Party Services",
                "body": """
                    <p>We use the following trusted third-party services that may handle your data on our behalf:</p>
                    <ul>
                        <li><strong>PayFast</strong> - payment processing (see <a href="https://payfast.io/legal/privacy-policy/" target="_blank" rel="noopener">PayFast Privacy Policy</a>)</li>
                        <li><strong>Brevo (Sendinblue)</strong> - transactional email delivery</li>
                        <li><strong>Cloudflare</strong> - website security and performance</li>
                    </ul>
                """
            },
            {
                "heading": "Your Rights",
                "body": """
                    <p>Under POPIA, you have the right to:</p>
                    <ul>
                        <li>Request access to the personal information we hold about you</li>
                        <li>Request correction of inaccurate information</li>
                        <li>Request deletion of your personal information (subject to legal obligations)</li>
                        <li>Object to the processing of your personal information</li>
                    </ul>
                    <p>To exercise any of these rights, please contact us at <a href="mailto:orders@ladycoleen.co.za">orders@ladycoleen.co.za</a>.</p>
                """
            },
            {
                "heading": "Cookies",
                "body": """
                    <p>Our website uses essential cookies to keep you logged in and to maintain your shopping cart.
                    We do not use advertising or tracking cookies.</p>
                """
            },
            {
                "heading": "Contact",
                "body": """
                    <p>For any privacy-related queries, contact us at <a href="mailto:orders@ladycoleen.co.za">orders@ladycoleen.co.za</a>.</p>
                """
            },
        ]
    )


@policies_bp.route("/policies/terms")
def terms():
    return render_template("policies/policy_page.html",
        title="Terms & Conditions",
        subtitle="Last updated: June 2025",
        sections=[
            {
                "heading": "Acceptance of Terms",
                "body": """
                    <p>By accessing and using the Lady Coleen Boutique Farmstall website (<strong>ladycoleen.co.za</strong>),
                    you agree to be bound by these Terms and Conditions. If you do not agree, please do not use this website.</p>
                """
            },
            {
                "heading": "Ordering",
                "body": """
                    <ul>
                        <li>All orders are subject to availability and confirmation of payment.</li>
                        <li>We reserve the right to refuse or cancel any order at our discretion, for example if a product is out of stock or there is an error in pricing.</li>
                        <li>Once an order is placed and payment is confirmed, you will receive an order confirmation email. This confirmation does not constitute a legally binding contract until the order is accepted and fulfilled.</li>
                        <li>Custom cake orders require additional confirmation of design details, sizing, and collection/delivery date before production begins.</li>
                    </ul>
                """
            },
            {
                "heading": "Pricing",
                "body": """
                    <p>All prices are displayed in <strong>South African Rand (ZAR)</strong> and include VAT where applicable.
                    Prices are subject to change without notice. The price charged will be the price displayed at the time of checkout.</p>
                """
            },
            {
                "heading": "Payment",
                "body": """
                    <p>Payment is processed securely via <strong>PayFast</strong>. We accept credit cards, debit cards, EFT, and other methods supported by PayFast.
                    We do not store any payment card details on our servers.</p>
                """
            },
            {
                "heading": "Intellectual Property",
                "body": """
                    <p>All content on this website, including text, images, logos, and designs, is the property of Lady Coleen Boutique Farmstall
                    and may not be reproduced without prior written permission.</p>
                """
            },
            {
                "heading": "Limitation of Liability",
                "body": """
                    <p>To the fullest extent permitted by law, Lady Coleen Boutique Farmstall shall not be liable for any indirect,
                    incidental, or consequential damages arising from the use of this website or our products.</p>
                """
            },
            {
                "heading": "Governing Law",
                "body": """
                    <p>These Terms and Conditions are governed by the laws of the <strong>Republic of South Africa</strong>.
                    Any disputes will be subject to the jurisdiction of the South African courts.</p>
                """
            },
            {
                "heading": "Changes to These Terms",
                "body": """
                    <p>We reserve the right to update these Terms and Conditions at any time. Changes will be posted on this page
                    with an updated revision date. Continued use of the website constitutes acceptance of the updated terms.</p>
                """
            },
            {
                "heading": "Contact",
                "body": """
                    <p>For any questions regarding these terms, contact us at <a href="mailto:orders@ladycoleen.co.za">orders@ladycoleen.co.za</a>.</p>
                """
            },
        ]
    )


@policies_bp.route("/policies/shipping")
def shipping_policy():
    return render_template("policies/policy_page.html",
        title="Shipping & Delivery Policy",
        subtitle="Last updated: June 2025",
        sections=[
            {
                "heading": "Delivery Options",
                "body": """
                    <p>We offer the following delivery methods:</p>
                    <ul>
                        <li><strong>Collection:</strong> Free collection from our farm stall. You will be notified by email when your order is ready.</li>
                        <li><strong>Local Delivery:</strong> We offer local delivery at a fee, which will be calculated at checkout based on your address.</li>
                        <li><strong>Pudo:</strong> We partner with <strong>Pudo</strong> for convenient locker-to-locker and door-to-door delivery across South Africa.</li>
                    </ul>
                """
            },
            {
                "heading": "Processing Times",
                "body": """
                    <ul>
                        <li><strong>Farm shop products:</strong> Orders are typically processed and ready within 1-2 business days.</li>
                        <li><strong>Custom cakes:</strong> Please allow a minimum of <strong>3-5 business days</strong> for custom cake orders. Rush orders may be accommodated at an additional fee - please contact us to check availability.</li>
                    </ul>
                """
            },
            {
                "heading": "Delivery Timeframes",
                "body": """
                    <ul>
                        <li><strong>Collection:</strong> Ready within 1-2 business days (farm products) or as agreed for custom cakes.</li>
                        <li><strong>Local Delivery:</strong> Within 1-3 business days of your order being ready.</li>
                        <li><strong>Pudo:</strong> Typically 2-5 business days depending on your location. A tracking number will be provided once dispatched.</li>
                    </ul>
                    <p>Please note that these are estimated timeframes and may vary during busy periods or public holidays.</p>
                """
            },
            {
                "heading": "Perishable Items",
                "body": """
                    <p>Custom cakes and perishable farm products are <strong>collection-preferred</strong>. If you select delivery for perishable items,
                    please ensure someone is available to receive the order to maintain product quality.
                    We cannot be held responsible for spoilage due to unsuccessful delivery attempts.</p>
                """
            },
            {
                "heading": "Shipping Costs",
                "body": """
                    <p>Shipping costs are calculated at checkout based on your chosen delivery method and location.
                    Collection is always free of charge.</p>
                """
            },
            {
                "heading": "Questions",
                "body": """
                    <p>For shipping enquiries, contact us at <a href="mailto:orders@ladycoleen.co.za">orders@ladycoleen.co.za</a>
                    or WhatsApp us via the contact details on our website.</p>
                """
            },
        ]
    )


@policies_bp.route("/policies/returns")
def returns_policy():
    return render_template("policies/policy_page.html",
        title="Returns & Exchange Policy",
        subtitle="Last updated: June 2025",
        sections=[
            {
                "heading": "Farm Shop Products",
                "body": """
                    <p>We want you to be completely satisfied with your purchase. If there is a problem with your order, we are here to help.</p>
                    <ul>
                        <li><strong>Damaged or incorrect items:</strong> Contact us within <strong>3 days</strong> of receiving your order. We will arrange a free exchange or full refund.</li>
                        <li><strong>Change of mind (non-perishable items):</strong> Unopened, non-perishable items may be returned within <strong>7 days</strong> of purchase in their original condition. Return shipping costs are the customer's responsibility unless the item was defective.</li>
                        <li><strong>Perishable goods:</strong> Due to the nature of fresh produce and food items, we cannot accept returns for change of mind on perishable goods.</li>
                    </ul>
                """
            },
            {
                "heading": "Custom Cakes",
                "body": """
                    <p>Custom cakes are made specifically to your order and cannot be exchanged or returned unless there is a quality or fulfilment error on our part.</p>
                    <ul>
                        <li>If your cake is significantly different from what was agreed (wrong flavour, design error, or damaged on arrival), please contact us within <strong>24 hours</strong> with photos.</li>
                        <li>We will work with you to find a fair resolution, which may include a partial or full refund, or a replacement order.</li>
                    </ul>
                """
            },
            {
                "heading": "How to Start a Return or Exchange",
                "body": """
                    <p>To initiate a return or exchange:</p>
                    <ol>
                        <li>Email us at <a href="mailto:orders@ladycoleen.co.za">orders@ladycoleen.co.za</a> with your order reference number and a description of the issue.</li>
                        <li>Attach photos of the item if it is damaged or incorrect.</li>
                        <li>We will respond within 1-2 business days with instructions.</li>
                    </ol>
                    <p>Please do not return items without first contacting us, as we may not be able to process unapproved returns.</p>
                """
            },
            {
                "heading": "Refunds on Returns",
                "body": """
                    <p>Once a return is approved and received, refunds will be processed to the original payment method within <strong>5-7 business days</strong>.
                    You will receive an email confirmation once your refund has been processed.</p>
                """
            },
        ]
    )
