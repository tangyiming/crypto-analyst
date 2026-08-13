plugins {
    id("com.android.application")
}

android {
    namespace = "app.cryptoanalyst"
    compileSdk = 35

    defaultConfig {
        applicationId = "app.cryptoanalyst"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
        debug {
            isDebuggable = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
