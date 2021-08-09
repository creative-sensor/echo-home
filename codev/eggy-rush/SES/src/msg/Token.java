/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package msg;

import java.io.CharArrayReader;
import java.io.IOException;
import java.util.Arrays;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.xml.parsers.ParserConfigurationException;
import javax.xml.parsers.SAXParserFactory;
import org.xml.sax.InputSource;
import org.xml.sax.SAXException;
import ses.EggyRush;
import xml.TokenXML;

/**
 *
 * @author Creativ
 */
public class Token {

    int cs;
    int[] SN; // sequence number
    int[] SV; // state vetor

    public Token() {
        this.SN = new int[EggyRush.PlayNum];
        this.SV = new int[EggyRush.PlayNum];
    }

    
    public Token(int cs, int[] tsn, int[] tsv) {
        this.cs = cs;
        this.SN = tsn;
        this.SV = tsv;
    }

    @Override
    public String toString() {
        String s = "<token>"
                + "<csid>" + this.cs + "</csid>"
                + "<tsn>" + Arrays.toString(this.SN) + "</tsn>"
                + "<tsv>" + Arrays.toString(this.SV) + "</tsv>";

        return s += "</token>\n";
    }

    public Token(String data) {
        try {

            SAXParserFactory factory = SAXParserFactory.newInstance();
            javax.xml.parsers.SAXParser saxparser = factory.newSAXParser();
            TokenXML txml = new TokenXML();
            saxparser.parse(new InputSource(new CharArrayReader(data.toCharArray())), txml);
            this.cs = txml.getCSID();
            this.SN = txml.getTSN();
            this.SV = txml.getTSV();

        } catch (IOException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);

        } catch (ParserConfigurationException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);
        } catch (SAXException ex) {
            Logger.getLogger(SESMessage.class.getName()).log(Level.SEVERE, null, ex);
            System.out.print(ex.getMessage());
        }
    }

    public int getCSID() {
        return this.cs;
    }
    
    public void setCSID(int id){
        this.cs = id;
    }

    public int[] SequenceNumber() {
        return this.SN;
    }

    public int[] StateVector() {
        return this.SV;
    }
}
