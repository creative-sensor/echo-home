import Amplify, { Auth } from 'aws-amplify';


Amplify.configure({
    Auth: {
        region: process.env.REGION,
        userPoolId: process.env.USER_POOL_ID,
        userPoolWebClientId: process.env.USER_POOL_WEB_CLIENT_ID,
        mandatorySignIn: false,
        clientMetadata: { myCustomKey: 'myCustomValue' }
    }
});

// You can get the current config object
const currentConfig = Auth.configure();

export function configs (args) {
        console.log(currentConfig);
}
