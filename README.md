# Gmail Sweeper

simple mail sweeper I made that works with your google accounts, ideally an N number of them because i was too tired to go through my personal and academic emails all the time while trying to make sense of it all.

Calls a local AI model to also summarize it in the terminal for easy reading and comprehension, pretty neat piece of work.

Entirely Claude Coded. No I dont have any shame. 

# Instructions

1. Go to Google Cloud Console, then make a new project dedicated to this. (if google asks for a billing account just make one, GMail API is completely free to call and you need to be a madman to rate-limit it.)
2. Go to Enable API and Services, then Library, search for the Gmail API, and then click on it and then click Enable.
3. Go to OAuth2.. something and then go Get Started -> fill out the stuff to your wish, ensure the app type is set to external.
4. Add yourself to the test users so that you don't need to hop through approval hoops.
5. Now go back and click on Credentials -> Create Credentials. Download the JSON file and rename it to "credentialX.json" where the X is 1,2,3.. and so on.
6. Add the label for this account in the .env.example, change the ollama model number if you so please (llama3.2:4b is fine tbh). Do ensure the model you have set is downloaded on your system.

Now when you launch this, the program will start authenticating however many emails you have set, then it will pull all unreads and WHAZAM! You've got mail!

# Bonus Features

This program tells IMAP to just peek at the emails, so whne you open the Gmail app it actually wont make the emails unread again, but keeps a local cache of read emails so that it doesnt re-read all unreads. Date set to the last time of sweep.
Does not apply any mail filters to a gmail account labelled "academic".
