/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package device;

import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.json.Json;
import javax.json.JsonObject;
import javax.json.JsonReader;


/**
 *
 * @author creativ
 */
public class Config {

    public static String rootpath = getRootPath();
    public static String fqdn = getItemServiceURL();
    public static int sectionSize = getSectionSize();
    public static int chunkSize = getChunkSize();
    
    private static String getRootPath(){
        FileInputStream conf = null;
        try {
            conf = new FileInputStream(System.getProperty("config"));
        } catch (FileNotFoundException ex) {
            Logger.getLogger(Config.class.getName()).log(Level.SEVERE, null, ex);
        }
        JsonReader jsonReader = Json.createReader(conf);
        JsonObject jsonObject = jsonReader.readObject();
        
        return jsonObject.getJsonString("root_path").getString();
        
        
    }
    
    private static String getItemServiceURL(){
         FileInputStream conf = null;
        try {
            conf = new FileInputStream(System.getProperty("config"));
        } catch (FileNotFoundException ex) {
            Logger.getLogger(Config.class.getName()).log(Level.SEVERE, null, ex);
        }
        JsonReader jsonReader = Json.createReader(conf);
        JsonObject jsonObject = jsonReader.readObject();
        
        return jsonObject.getJsonString("item_service_url").getString();
    }
    
    private static int getSectionSize(){
         FileInputStream conf = null;
        try {
            conf = new FileInputStream(System.getProperty("config"));
        } catch (FileNotFoundException ex) {
            Logger.getLogger(Config.class.getName()).log(Level.SEVERE, null, ex);
        }
        JsonReader jsonReader = Json.createReader(conf);
        JsonObject jsonObject = jsonReader.readObject();
        
        return jsonObject.getJsonNumber("section_capacity").intValue();
    }
    
    private static int getChunkSize(){
         FileInputStream conf = null;
        try {
            conf = new FileInputStream(System.getProperty("config"));
        } catch (FileNotFoundException ex) {
            Logger.getLogger(Config.class.getName()).log(Level.SEVERE, null, ex);
        }
        JsonReader jsonReader = Json.createReader(conf);
        JsonObject jsonObject = jsonReader.readObject();
        
        return jsonObject.getJsonNumber("chunk_size").intValue();
    }
    
    
}
