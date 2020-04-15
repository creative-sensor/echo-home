import { Auth }  from 'aws-amplify';
import { authenticator } from 'otplib'
import { configs } from './configure';


//----------------  CONFIG INPUT------------------------
var username = process.env.USER_NAME;
var password = process.env.PASSWORD;
var secret = process.env.SECRET;
//------------------------------------------------


let code = '000000';
var validationData = null;

function genCode() {
    // Alternatively: const secret = otplib.authenticator.generateSecret();
    const token = authenticator.generate(secret);
    code = token;
}


function getCodeFromStdInput () {
    console.log("Input OTP code:");
    var readline = require('readline');
    var stdInput = readline.createInterface({
        input: process.stdin,
        output: process.stdout,
        terminal: false
    });
    var setCode = (line) => {
        console.log(`code in closure ${code}`);
        code = line;
        console.log(`code in closure set line: ${code}`);
    }
    stdInput.on('line', setCode);
}


async function SignIn() {
    try {
        const user =  await Auth.signIn(username, password);
        if (user.challengeName === 'SMS_MFA' ||
            user.challengeName === 'SOFTWARE_TOKEN_MFA') {
            // You need to get the code from the UI inputs
            // and then trigger the following function with a button click

            // If MFA is enabled, sign-in should be confirmed with the confirmation code
            const loggedUser =  await Auth.confirmSignIn(
                user,   // Return object from Auth.signIn()
                code,   // Confirmation code  
                user.challengeName // MFA Type e.g. SMS_MFA, SOFTWARE_TOKEN_MFA
            );
            //console.log(loggedUser.signInUserSession.CognitoUserSession.idToken.refreshToken.accessToken);
            console.log(loggedUser.signInUserSession.idToken.jwtToken);
        } else if (user.challengeName === 'NEW_PASSWORD_REQUIRED') {
            const {requiredAttributes} = user.challengeParam; // the array of required attributes, e.g ['email', 'phone_number']
            // You need to get the new password and required attributes from the UI inputs
            // and then trigger the following function with a button click
            // For example, the email and phone_number are required attributes
            const {username, email, phone_number} = getInfoFromUserInput();
            const loggedUser = await Auth.completeNewPassword(
                user,              // the Cognito User Object
                newPassword,       // the new password
                // OPTIONAL, the required attributes
                {
                    email,
                    phone_number,
                }
            );
        } else if (user.challengeName === 'MFA_SETUP') {
            // This happens when the MFA method is TOTP
            // The user needs to setup the TOTP before using it
            // More info please check the Enabling MFA part
            Auth.setupTOTP(user);
        } else {
            // The user directly signs in
            console.log("Directly signed in");
            console.log(user);
        }
    } catch (err) {
        if (err.code === 'UserNotConfirmedException') {
            // The error happens if the user didn't finish the confirmation step when signing up
            // In this case you need to resend the code and confirm the user
            // About how to resend the code and confirm the user, please check the signUp part
        } else if (err.code === 'PasswordResetRequiredException') {
            // The error happens when the password is reset in the Cognito console
            // In this case you need to call forgotPassword to reset the password
            // Please check the Forgot Password part.
        } else if (err.code === 'NotAuthorizedException') {
            // The error happens when the incorrect password is provided
        } else if (err.code === 'UserNotFoundException') {
            // The error happens when the supplied username/email does not exist in the Cognito user pool
        } else {
            console.log(err);
        }
    }
}
// For advanced usage
// You can pass an object which has the username, password and validationData which is sent to a PreAuthentication Lambda trigger

export function cmd () {
    genCode();

    SignIn({
        username, // Required, the username
        password, // Optional, the password
        validationData, // Optional, a random key-value pair map which can contain any key and will be passed to your PreAuthentication Lambda trigger as-is. It can be used to implement additional validations around authentication
    })
}

