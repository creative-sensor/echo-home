/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package device;

import entity.Mp3item;
import java.io.File;
import java.io.IOException;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.ejb.EJB;
import javax.naming.Context;
import javax.naming.InitialContext;
import javax.naming.NamingException;
import javax.servlet.ServletException;
import javax.servlet.annotation.WebServlet;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import org.apache.commons.io.FileUtils;
import db.facades.Mp3itemFacadeLocal;

/**
 *
 * @author creativ
 */
@WebServlet(name = "GetItem", urlPatterns = {"/GetItem"})
public class ItemRetrieval extends HttpServlet {
    @EJB
    private Mp3itemFacadeLocal mp3itemFacade = lookupMp3itemFacadeLocal();
    

    /**
     * Processes requests for both HTTP
     * <code>GET</code> and
     * <code>POST</code> methods.
     *
     * @param request servlet request
     * @param response servlet response
     * @throws ServletException if a servlet-specific error occurs
     * @throws IOException if an I/O error occurs
     */
    protected void processRequest(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        String id = request.getParameter("id");
        response.setContentType("audio/mpeg");
        
        Mp3item item = mp3itemFacade.find(Integer.parseInt(id));
        String path = item.getPath();
        
        byte[] bytestr = FileUtils.readFileToByteArray(new File(Config.rootpath + path));
        response.setContentLength(bytestr.length);
        
        int chunk_size = Config.chunkSize; // byte
        int chunk_number = bytestr.length / chunk_size;
        int chunk_remain = bytestr.length % chunk_size;
        
        for(int i=0; i<chunk_number; i++){
            response.getOutputStream().write(bytestr, i*chunk_size, chunk_size);
        }
        
        response.getOutputStream().write(bytestr, chunk_number*chunk_size, chunk_remain);
         // loop write method to get rid of timer
        
    }

    private Mp3itemFacadeLocal lookupMp3itemFacadeLocal() {
        try {
            Context c = new InitialContext();
            return (Mp3itemFacadeLocal) c.lookup("java:global/RemoteMP3Player/Mp3itemFacade!db.facades.Mp3itemFacadeLocal");
        } catch (NamingException ne) {
            Logger.getLogger(getClass().getName()).log(Level.SEVERE, "exception caught", ne);
            throw new RuntimeException(ne);
        }
    }
    // <editor-fold defaultstate="collapsed" desc="HttpServlet methods. Click on the + sign on the left to edit the code.">
    /**
     * Handles the HTTP
     * <code>GET</code> method.
     *
     * @param request servlet request
     * @param response servlet response
     * @throws ServletException if a servlet-specific error occurs
     * @throws IOException if an I/O error occurs
     */
    @Override
    protected void doGet(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        processRequest(request, response);
    }

    /**
     * Handles the HTTP
     * <code>POST</code> method.
     *
     * @param request servlet request
     * @param response servlet response
     * @throws ServletException if a servlet-specific error occurs
     * @throws IOException if an I/O error occurs
     */
    @Override
    protected void doPost(HttpServletRequest request, HttpServletResponse response)
            throws ServletException, IOException {
        processRequest(request, response);
    }

    /**
     * Returns a short description of the servlet.
     *
     * @return a String containing servlet description
     */
    @Override
    public String getServletInfo() {
        return "Short description";
    }// </editor-fold>
    
    
}
