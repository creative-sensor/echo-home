/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ses;


import java.util.Iterator;
import java.util.LinkedList;

/**
 *
 * @author Creativ
 */
public class PairVector extends LinkedList<Pair> {

//    public PairVector(String data) {
//        this.list = new LinkedList<>();
//        try {
//
//            SAXParserFactory factory = SAXParserFactory.newInstance();
//            javax.xml.parsers.SAXParser saxparser = factory.newSAXParser();
//            VMXML vmxml = new VMXML();
//            saxparser.parse(new InputSource(new CharArrayReader(data.toCharArray())),vmxml);
//
//            
//        } catch (IOException ex) {
//            Logger.getLogger(IncomingMessage.class.getName()).log(Level.SEVERE, null, ex);
//
//        } catch (ParserConfigurationException ex) {
//            Logger.getLogger(IncomingMessage.class.getName()).log(Level.SEVERE, null, ex);
//        } catch (SAXException ex) {
//            Logger.getLogger(IncomingMessage.class.getName()).log(Level.SEVERE, null, ex);
//        }   
//    }

    
    public String toString() {
        String content = null;
        for(int i=0; i<this.size(); i++){
            content += this.get(i).toString();
        }
        return "<vm>" + content + "</vm>";
    }
    
    public int hasEntry(int pid){
        for (int i=0 ;i<this.size(); i++){
            if(this.get(i).getID() == pid){
                return i;
            }
        }
        
        return -1;
    }
    
    public String toLog(){
        String logstring = new String();
        Iterator<Pair> iter = this.iterator();
        while(iter.hasNext()){
            logstring += iter.next().toLog();
        }
        
        return logstring;
    }
            
}
